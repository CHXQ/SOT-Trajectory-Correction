from datasets import points_utils
from models import my_base_model
from models.backbone.pointnet_new import Pointnet_Backbone, MiniPointNet, SegPointNet
from utils.metrics import estimateOverlap, estimateAccuracy
import torch
from torch import nn
import torch.nn.functional as F
from ipdb import set_trace
from nuscenes.utils.data_classes import LidarPointCloud
from datasets.data_classes import PointCloud, Box
import numpy as np
import os
import pickle
import copy

class MyModel(my_base_model.BaseModel):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        
        self.T = 41
        self.feature_num = 512
        self.save_flag = False
        self.output = []
        self.search_scale = getattr(config, 'bb_scale', 1.0)
        self.search_offset = getattr(config, 'bb_offset', 0)
        self.max_frame_num = getattr(config, 'max_frame_num', 0)
        self.max_point_num = getattr(config, 'max_point_num', 0)
        self.point_sample_size = getattr(config, 'point_sample_size', 0)
        self.seg_pointnet = SegPointNet(input_channel=3 + 1,
                                        per_point_mlp1=[64, 64, 64, 128, 1024],
                                        per_point_mlp2=[512, 256, 128, 128],
                                        output_size=2)
        self.pointnet2 = Pointnet_Backbone(input_channels = 1)
        self.mini_pointnet = MiniPointNet(input_channel=3 + 1,
                                          per_point_mlp=[64, 128, 256, 512],
                                          hidden_mlp=[512, 256],
                                          output_size=-1)
        
        # self.box_mlp = nn.Sequential(nn.Linear(256, 256),
        #                                 nn.BatchNorm1d(256),
        #                                 nn.ReLU(),
        #                                 # nn.Linear(256, 128),
        #                                 # nn.BatchNorm1d(128),
        #                                 # nn.ReLU(),
        #                                 nn.Linear(256, self.T * 4))
        
        self.mlp = nn.Sequential(
                                 nn.Flatten(),
                                 nn.Linear(self.T * 2 * self.feature_num, 2048),
                                 nn.BatchNorm1d(2048),
                                 nn.ReLU(),
                                 nn.Linear(2048, self.T * 4))
        
    def forward(self, input_dict):
        """
        Args:
            input_dict: {
            "points": (B,N,3+1)
            }
        """
        output_dict = {}
        
        # x1 = input_dict["pc_data"].cuda()
        # B, N, _ = x1.shape
        # _, point_feature, _ = self.pointnet2(x1, [1024, 256, 41])
        
        x = input_dict["pc_data"].cuda()
        
        # seg
        x = x.transpose(1, 2)
        B, _, N = x.shape
        seg_out = self.seg_pointnet(x)
        seg_logits = seg_out[:, :2, :]  # B,2,N
        pred_cls = torch.argmax(seg_logits, dim=1, keepdim=True)  # B,1,N
        mask_points = x[:, :4, :] * pred_cls
        mask_points = mask_points.transpose(1, 2)
        _, point_feature, _ = self.pointnet2(mask_points, [1024, 256, 41])
        
        # x2 = input_dict["track_bbox"].transpose(1,2).cuda()
        # bbox_feature = self.mini_pointnet(x2)
        
        # x = torch.cat((point_feature, bbox_feature), 1)
        
        x = torch.cat((point_feature, point_feature), 1)
        output_offset = self.mlp(x)
        
        # point_feature = self.mini_pointnet(mask_points)
        # x2 = input_dict["track_bbox"].transpose(1,2).cuda()
        # bbox_feature = self.mini_pointnet(x2)
        # x = torch.cat((point_feature, bbox_feature), 1)
        # output_offset = self.box_mlp(point_feature)
        track_bbox = input_dict['track_bbox'].view((-1, 4))
        output_offset = output_offset.view((-1, 4))
        output_offset[:, 3] = 0
        output = track_bbox + output_offset
        output_dict['estimation_boxes'] = output
        output_dict['seg_logits'] = seg_logits
        
        return output_dict
    
    def compute_loss(self, data, output):
        loss_total = 0.0
        loss_dict = {}
        seg_logits = output['seg_logits']
        estimation_boxes = output['estimation_boxes'][:, :3] # B,4
        with torch.no_grad():
            box_label = data['gt_track_bbox'].view(-1, 4)
            frame_num = data['frame_num']
            center_label = box_label[:, :3]
            seg_label = data['seg_label']
       
        bbox_mask = data['bbox_mask'].view(-1, 1)
        estimation_boxes = estimation_boxes * bbox_mask
        center_label = center_label * bbox_mask
        bbox_mask = bbox_mask.squeeze()
        estimation_boxes = estimation_boxes[bbox_mask>0]
        center_label = center_label[bbox_mask>0]
        loss_center = F.smooth_l1_loss(estimation_boxes, center_label)
        loss_seg = F.cross_entropy(seg_logits, seg_label, weight=torch.tensor([0.5, 2.0]).cuda())
        loss_total = 0.1 * loss_seg + loss_center
        # loss_angle = F.smooth_l1_loss(torch.sin(estimation_boxes[:, 3]), angle_label)
        # loss_total += loss_center * self.config.center_weight + loss_angle * self.config.angle_weight
        # loss_dict["loss_center"] = loss_center
        # loss_dict["loss_angle"] = loss_angle
        loss_dict['loss_total'] = loss_total
        loss_dict["loss_center"] = loss_center
        loss_dict['loss_seg'] = loss_seg
        # loss_dict['loss_total'] = loss_center
        
        return loss_dict
        
    def training_step(self, batch, batch_idx):
        output = self(batch)
        loss_dict = self.compute_loss(batch, output)
        loss = loss_dict['loss_total']
        
        log_dict = {k: v.item() for k, v in loss_dict.items()}

        self.logger.experiment.add_scalars('loss', log_dict,
                                           global_step=self.global_step)
        return loss
    
    def evaluate_one_sequence(self, sequence):
        """
        :param sequence: a sequence of annos {"pc": pc, "3d_bbox": bb, 'meta': anno}
        :return:
        """
        ious = []
        distances = []
        results_bbs = []
        
        bbox_label = sequence['gt_track']
        
        # bbox_corners = [bbs.corners() for bbs in sequence['track']]
        # np.save('bus_bbox0.npy', np.array(bbox_corners))
        
        input_dict = self.build_input_dict(sequence)
        ref_box = input_dict['ref_box']
        output = self(input_dict)
        estimation_box = output['estimation_boxes']
        estimation_box_cpu = estimation_box.cpu()
        estimation_box_cpu[:, 3] = input_dict['track_bbox'][0][:, 3]
        # estimation_box_cpu = input_dict['track_bbox'][0].cpu()
        
        for i in range(len(bbox_label)):
            
            if (i == 0):
                output_bbox = bbox_label[i]
            else:
                output_bbox = points_utils.getOffsetBB(ref_box, estimation_box_cpu[i], degrees=self.config.degrees,
                                                 use_z=self.config.use_z,
                                                 limit_box=self.config.limit_box)
            
            this_overlap = estimateOverlap(bbox_label[i], output_bbox, dim=self.config.IoU_space,
                                           up_axis=self.config.up_axis)
            this_accuracy = estimateAccuracy(bbox_label[i], output_bbox, dim=self.config.IoU_space,
                                             up_axis=self.config.up_axis)
            ious.append(this_overlap)
            distances.append(this_accuracy)
            results_bbs.append(output_bbox)
        
        # save result
        # pc = input_dict['pc_data'].view(-1, 4)[:, :3].cpu()
        # np.save('pc_data0', np.array(pc))
        # gt_corners = [bbs.corners() for bbs in bbox_label]
        # result_corners = [bbs.corners() for bbs in results_bbs]
        # np.save('bus_gt_3d_bbox0.npy', np.array(gt_corners))
        # np.save('result_bbs0.npy', np.array(result_corners))
        
        return ious, distances, results_bbs

    def validation_step(self, batch, batch_idx):
        sequence = batch[0]  # unwrap the batch with batch size = 1
        ious, distances, *_ = self.evaluate_one_sequence(sequence)
        # update metrics
        self.success(torch.tensor(ious, device=self.device))
        self.prec(torch.tensor(distances, device=self.device))
        self.log('success/test', self.success, on_step=True, on_epoch=True)
        self.log('precision/test', self.prec, on_step=True, on_epoch=True)

    def validation_epoch_end(self, outputs):
        # print('val')
        self.logger.experiment.add_scalars('metrics/test',
                                           {'success': self.success.compute(),
                                            'precision': self.prec.compute()},
                                           global_step=self.global_step)
    
    def test_step(self, batch, batch_idx):
        sequence = batch[0]  # unwrap the batch with batch size = 1
        ious, distances, result_bbs = self.evaluate_one_sequence(sequence)           
        # update metrics
        self.success(torch.tensor(ious, device=self.device))
        self.prec(torch.tensor(distances, device=self.device))
        self.log('success/test', self.success, on_step=True, on_epoch=True)
        self.log('precision/test', self.prec, on_step=True, on_epoch=True)
        if self.save_flag:
            sequence['track'] = result_bbs
            self.output.append(sequence)
        
        return result_bbs

    def test_epoch_end(self, outputs):
        if self.save_flag:
            save_output_path = '/home/zhangxq/datasets/nuscenes/nuscenes_track_car_val_bbox_iter1.pkl'
            with open(save_output_path, 'wb') as f:
                pickle.dump(self.output, f, 0)
        self.logger.experiment.add_scalars('metrics/test',
                                        {'success': self.success.compute(),
                                         'precision': self.prec.compute()},
                                        global_step=self.global_step)
    
    
    
    def build_input_dict(self, info):
        gt_track = info['gt_track']
        track = info['track']
        pc_paths = info['lidar_path']
        frame_num = info['frame_num']
        pc_data = []
        centers = []
        track_bbox = []
        track_corners = []
        origin_pc_data = []
        begin_sot_box = gt_track[0]

        # define ref_center and ref_rot_mat
        for box in track:
            centers.append(box.center)
        centers = np.array(centers)
        
        center_range = np.stack((np.min(centers, axis=0), np.max(centers, axis=0)), axis = 0)
        # ref_center = np.mean(center_range, axis=0)
        # ref_box = Box(center=ref_center, size=begin_sot_box.wlh, orientation=begin_sot_box.orientation)
        ref_box = begin_sot_box
        ref_center = ref_box.center
        # ref_box_theta = ref_box.orientation.radians * ref_box.orientation.axis[-1]
        # ref_bbox = np.append(ref_box.center, ref_box_theta).astype('float32')
        center_range -= ref_center
        range_len = center_range[1, :] - center_range[0, :]
        
        # get all frames point cloud data
        for i, pc_path in enumerate(pc_paths):
            pc = LidarPointCloud.from_file(pc_path)
            # coordinate trans: lidar to ego
            pc.rotate(info['lidar2ego_rotation'][i].rotation_matrix)
            pc.translate(info['lidar2ego_translation'][i])
            # coordinate trans: ego to global
            pc.rotate(info['ego2global_rotation'][i].rotation_matrix)
            pc.translate(info['ego2global_translation'][i])

            pc = PointCloud(points=pc.points)
            crop_pc = points_utils.generate_subwindow(pc, track[i], scale=self.search_scale, offset=self.search_offset)
            # assert crop_pc.nbr_points() > 20, 'not enough search points'
            crop_pc.rotate(track[i].rotation_matrix)
            crop_pc.translate(track[i].center)
            origin_pc = copy.deepcopy(crop_pc)
            crop_pc = points_utils.transform_pc(crop_pc, ref_box)
            points, _ = points_utils.regularize_pc(crop_pc.points.T, self.point_sample_size)
            origin_pc, _ = points_utils.regularize_pc(origin_pc.points.T, self.point_sample_size)
            timestamp = np.full((points.shape[0], 1), fill_value = i)
            pc_data.append(np.concatenate([points, timestamp], axis=-1))
            origin_pc_data.append(np.concatenate([origin_pc, timestamp], axis=-1))

            track[i] = points_utils.transform_box(track[i], ref_box)
            theta = track[i].orientation.radians * track[i].orientation.axis[-1]
            # box = np.append((track[i].center - center_range[0]) / range_len, theta).astype('float32')
            box = np.append(track[i].center, theta).astype('float32')
            track_bbox.append(box)
            track_corners.append(track[i].corners())
        
        # padding
        pc_data = np.concatenate(pc_data).astype('float32')
        pc_data = np.pad(pc_data, ((0, self.max_point_num - len(pc_data)), (0, 0)), 'constant', constant_values = (0, 0))
        
        origin_pc_data = np.concatenate(origin_pc_data).astype('float32')
        origin_pc_data = np.pad(origin_pc_data, ((0, self.max_point_num - len(pc_data)), (0, 0)), 'constant', constant_values = (0, 0))
        
        track_bbox = np.array(track_bbox)
        track_bbox = np.pad(track_bbox, ((0, self.max_frame_num - frame_num), (0, 0)), 'constant', constant_values = (0, 0))
        
        begin_sot_box = points_utils.transform_box(begin_sot_box, ref_box)
        begin_sot_box_theta = begin_sot_box.orientation.radians * begin_sot_box.orientation.axis[-1]
        begin_sot_bbox = np.append(begin_sot_box.center, begin_sot_box_theta).astype('float32')
        
        # pc_data, _ = points_utils.regularize_pc(pc_data, self.point_sample_size)
        input_dict = {
            'origin_pc': torch.tensor(origin_pc_data[None, ...], device=self.device),
            'pc_data': torch.tensor(pc_data[None, ...], device=self.device),
            'track_bbox': torch.tensor(track_bbox[None, ...], device=self.device),
            'ref_box': ref_box,
            # 'begin_sot_bbox': begin_sot_bbox,
            # 'bbox_size': begin_sot_box.wlh,
            # 'track_corners': np.array(track_corners),
            # 'track_gt_corners': np.array(track_gt_corners),
            'center_range': center_range,
            'range_len': range_len
        }
        return input_dict