#########################################################################
##
## train agent that has some utility for training and saving.
##
#########################################################################

import torch.nn as nn
import torch
from util_hourglass import *
from copy import deepcopy
import numpy as np
from torch.autograd import Variable
from hourglass_network import lane_detection_network
from torch.autograd import Function as F
from parameters import Parameters
import math
import util

############################################################
##
## agent for lane detection
##
############################################################
class Agent(nn.Module):

    #####################################################
    ## Initialize
    #####################################################
    def __init__(self):
        super(Agent, self).__init__()

        self.p = Parameters()

        self.lane_detection_network = lane_detection_network()

        self.setup_optimizer()

        self.current_epoch = 0

    def count_parameters(self, model):
	    return sum(p.numel() for p in model.parameters() if p.requires_grad)

    def setup_optimizer(self):
        self.lane_detection_optim = torch.optim.Adam(self.lane_detection_network.parameters(),
                                                    lr=self.p.l_rate,
                                                    weight_decay=self.p.weight_decay)

    #####################################################
    ## Make ground truth for key point estimation
    #####################################################
    def make_ground_truth_point(self, target_lanes, target_h):

        target_lanes, target_h = util.sort_batch_along_y(target_lanes, target_h)

        ground = np.zeros((len(target_lanes), 3, self.p.grid_y, self.p.grid_x))
        ground_binary = np.zeros((len(target_lanes), 1, self.p.grid_y, self.p.grid_x))

        for batch_index, batch in enumerate(target_lanes):
            for lane_index, lane in enumerate(batch):
                for point_index, point in enumerate(lane):
                    if point > 0:
                        x_index = int(point/self.p.resize_ratio)
                        y_index = int(target_h[batch_index][lane_index][point_index]/self.p.resize_ratio)
                        ground[batch_index][0][y_index][x_index] = 1.0
                        ground[batch_index][1][y_index][x_index]= (point*1.0/self.p.resize_ratio) - x_index
                        ground[batch_index][2][y_index][x_index] = (target_h[batch_index][lane_index][point_index]*1.0/self.p.resize_ratio) - y_index
                        ground_binary[batch_index][0][y_index][x_index] = 1

        return ground, ground_binary


    #####################################################
    ## Make ground truth for instance feature
    #####################################################
    def make_ground_truth_instance(self, target_lanes, target_h):

        ground = np.zeros((len(target_lanes), 1, self.p.grid_y*self.p.grid_x, self.p.grid_y*self.p.grid_x))

        for batch_index, batch in enumerate(target_lanes):
            temp = np.zeros((1, self.p.grid_y, self.p.grid_x))
            lane_cluster = 1
            for lane_index, lane in enumerate(batch):
                previous_x_index = 0
                previous_y_index = 0
                for point_index, point in enumerate(lane):
                    if point > 0:
                        x_index = int(point/self.p.resize_ratio)
                        y_index = int(target_h[batch_index][lane_index][point_index]/self.p.resize_ratio)
                        temp[0][y_index][x_index] = lane_cluster
                    if previous_x_index != 0 or previous_y_index != 0: #interpolation make more dense data
                        temp_x = previous_x_index
                        temp_y = previous_y_index
                        while True:
                            delta_x = 0
                            delta_y = 0
                            temp[0][temp_y][temp_x] = lane_cluster
                            if temp_x < x_index:
                                temp[0][temp_y][temp_x+1] = lane_cluster
                                delta_x = 1
                            elif temp_x > x_index:
                                temp[0][temp_y][temp_x-1] = lane_cluster
                                delta_x = -1
                            if temp_y < y_index:
                                temp[0][temp_y+1][temp_x] = lane_cluster
                                delta_y = 1
                            elif temp_y > y_index:
                                temp[0][temp_y-1][temp_x] = lane_cluster
                                delta_y = -1
                            temp_x += delta_x
                            temp_y += delta_y
                            if temp_x == x_index and temp_y == y_index:
                                break
                    if point > 0:
                        previous_x_index = x_index
                        previous_y_index = y_index
                lane_cluster += 1

            for i in range(self.p.grid_y*self.p.grid_x): #make gt
                temp = temp[temp>-1]
                gt_one = deepcopy(temp)
                if temp[i]>0:
                    gt_one[temp==temp[i]] = 1   #same instance
                    if temp[i] == 0:
                        gt_one[temp!=temp[i]] = 3 #different instance, different class
                    else:
                        gt_one[temp!=temp[i]] = 2 #different instance, same class
                        gt_one[temp==0] = 3 #different instance, different class
                    ground[batch_index][0][i] += gt_one

        return ground

    #####################################################
    ## train
    #####################################################
    def train(self, inputs, target_lanes, target_h, epoch, agent):
        point_loss = self.train_point(inputs, target_lanes, target_h, epoch)
        return point_loss

    #####################################################
    ## compute loss function and optimize
    #####################################################
    def train_point(self, inputs, target_lanes, target_h, epoch):
        real_batch_size = len(target_lanes)

        #generate ground truth
        ground_truth_point, ground_binary = self.make_ground_truth_point(target_lanes, target_h)
        ground_truth_instance = self.make_ground_truth_instance(target_lanes, target_h)

        # convert numpy array to torch tensor
        ground_truth_point = torch.from_numpy(ground_truth_point).float()
        ground_truth_point = Variable(ground_truth_point).cuda()
        ground_truth_point.requires_grad=False

        ground_binary = torch.LongTensor(ground_binary.tolist()).cuda()
        ground_binary.requires_grad=False

        ground_truth_instance = torch.from_numpy(ground_truth_instance).float()
        ground_truth_instance = Variable(ground_truth_instance).cuda()
        ground_truth_instance.requires_grad=False

        #util.visualize_gt(ground_truth_point[0], ground_truth_instance[0], inputs[0])

        # update lane_detection_network
        result = self.predict_lanes(inputs)
        lane_detection_loss = 0
        for (confidance, offset, feature) in result:
            #compute loss for point prediction
            offset_loss = 0
            exist_condidence_loss = 0
            nonexist_confidence_loss = 0

            #exist confidance loss
            confidance_gt = ground_truth_point[:, 0, :, :]
            confidance_gt = confidance_gt.view(real_batch_size, 1, self.p.grid_y, self.p.grid_x)
            exist_condidence_loss = torch.sum(    (confidance_gt[confidance_gt==1] - confidance[confidance_gt==1])**2      )/torch.sum(confidance_gt==1)

            #non exist confidance loss
            nonexist_confidence_loss = torch.sum(    (confidance_gt[confidance_gt==0] - confidance[confidance_gt==0])**2      )/torch.sum(confidance_gt==0)

            #offset loss 
            offset_x_gt = ground_truth_point[:, 1:2, :, :]
            offset_y_gt = ground_truth_point[:, 2:3, :, :]

            predict_x = offset[:, 0:1, :, :]
            predict_y = offset[:, 1:2, :, :]

            x_offset_loss = torch.sum( (offset_x_gt[confidance_gt==1] - predict_x[confidance_gt==1])**2 )/torch.sum(confidance_gt==1)
            y_offset_loss = torch.sum( (offset_y_gt[confidance_gt==1] - predict_y[confidance_gt==1])**2 )/torch.sum(confidance_gt==1)

            offset_loss = (x_offset_loss + y_offset_loss)/2

            #compute loss for similarity
            sisc_loss = 0
            disc_loss = 0

            feature_map = feature.view(real_batch_size, self.p.feature_size, 1, self.p.grid_y*self.p.grid_x)
            feature_map = feature_map.expand(real_batch_size, self.p.feature_size, self.p.grid_y*self.p.grid_x, self.p.grid_y*self.p.grid_x).detach()

            point_feature = feature.view(real_batch_size, self.p.feature_size, self.p.grid_y*self.p.grid_x,1)
            point_feature = point_feature.expand(real_batch_size, self.p.feature_size, self.p.grid_y*self.p.grid_x, self.p.grid_y*self.p.grid_x)#.detach()

            distance_map = (feature_map-point_feature)**2 
            distance_map = torch.norm( distance_map, dim=1 ).view(real_batch_size, 1, self.p.grid_y*self.p.grid_x, self.p.grid_y*self.p.grid_x)

            # same instance
            sisc_loss = torch.sum(distance_map[ground_truth_instance==1])/torch.sum(ground_truth_instance==1)

            # different instance, same class
            disc_loss = self.p.K1-distance_map[ground_truth_instance==2] #self.p.K1/distance_map[ground_truth_instance==2] + (self.p.K1-distance_map[ground_truth_instance==2])
            disc_loss[disc_loss<0] = 0
            disc_loss = torch.sum(disc_loss)/torch.sum(ground_truth_instance==2)

            print("seg loss################################################################")
            print(sisc_loss)
            print(disc_loss)

            print("point loss")
            print(exist_condidence_loss)
            print(nonexist_confidence_loss)
            print(offset_loss)

            print("lane loss")
            lane_loss = self.p.constant_exist*exist_condidence_loss + self.p.constant_nonexist*nonexist_confidence_loss + self.p.constant_offset*offset_loss
            print(lane_loss)

            print("instance loss")
            instance_loss = self.p.constant_alpha*sisc_loss + self.p.constant_beta*disc_loss
            print(instance_loss)

            lane_detection_loss = lane_detection_loss + self.p.constant_lane_loss*lane_loss + self.p.constant_instance_loss*instance_loss

        self.lane_detection_optim.zero_grad()
        lane_detection_loss.backward()
        self.lane_detection_optim.step()

        del confidance, offset, feature
        del ground_truth_point, ground_binary, ground_truth_instance
        del feature_map, point_feature, distance_map
        del exist_condidence_loss, nonexist_confidence_loss, offset_loss, sisc_loss, disc_loss, lane_loss, instance_loss

        if epoch>0 and epoch%20==0 and self.current_epoch != epoch:
            self.current_epoch = epoch
            if epoch>0 and (epoch == 1000):
			self.p.constant_lane_loss += 0.5
			self.p.constant_nonexist += 0.5
			self.p.l_rate /= 2.0
			self.setup_optimizer()
		
        return lane_detection_loss

    #####################################################
    ## predict lanes
    #####################################################
    def predict_lanes(self, inputs):
        inputs = torch.from_numpy(inputs).float() 
        inputs = Variable(inputs).cuda()

        return self.lane_detection_network(inputs)

    #####################################################
    ## predict lanes in test
    #####################################################
    def predict_lanes_test(self, inputs):
        inputs = torch.from_numpy(inputs).float() 
        inputs = Variable(inputs).cuda()

        return self.lane_detection_network(inputs)

    #####################################################
    ## Training mode
    #####################################################                                                
    def training_mode(self):
        self.lane_detection_network.train()

    #####################################################
    ## evaluate(test mode)
    #####################################################                                                
    def evaluate_mode(self):
        self.lane_detection_network.eval()

    #####################################################
    ## Setup GPU computation
    #####################################################                                                
    def cuda(self):
        self.lane_detection_network.cuda()

    #####################################################
    ## Load save file
    #####################################################
    def load_weights(self, epoch, loss):
        self.lane_detection_network.load_state_dict(
            torch.load(self.p.model_path+str(epoch)+'_'+str(loss)+'_'+'lane_detection_network.pkl'),False
        )

    #####################################################
    ## Save model
    #####################################################
    def save_model(self, epoch, loss):
        torch.save(
            self.lane_detection_network.state_dict(),
            self.p.save_path+str(epoch)+'_'+str(loss)+'_'+'lane_detection_network.pkl'
        )
