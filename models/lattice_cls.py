import torch.nn as nn
import torch.utils.data
import torch.nn.functional as F
# from pointnet import PointNetEncoder, feature_transform_reguliarzer

import math

from resnet import resnet50
from trans_mat_net import TransMatNet


class SparseSum(torch.autograd.Function):
    @staticmethod
    def forward(ctx, indices, values, size, cuda):
        """

        :param ctx:
        :param indices: (1, B*d1*N)
        :param values: (B*d1*N, feat_size)
        :param size: (B*(H+1), feat_size)
        :param cuda: bool
        :return: (B*(H+1), feat_size)
        """

        ctx.save_for_backward(indices)

        if cuda:
            output = torch.cuda.sparse.FloatTensor(indices, values, size)
        else:
            output = torch.sparse.FloatTensor(indices, values, size)

        output = output.to_dense()
        return output

    @staticmethod
    def backward(ctx, grad_output):



        # kaidong test
        import pdb; pdb.set_trace()

        indices, = ctx.saved_tensors

        grad_values = None
        if ctx.needs_input_grad[1]:
            grad_values = grad_output[indices.squeeze(0), :]

        return None, grad_values, None, None


sparse_sum = SparseSum.apply

class get_model(nn.Module):

    # kaidong: test
    # s is the final image scale
    def __init__(self, k=40, num_pts=1024, normal_channel=True, s=128*3):
        super(get_model, self).__init__()

        # self.lat_transform = LatticeGen(s)
        self.mat_gen = TransMatNet(6, num_pts, 3, 3)
        self.lat_transform = LatticeGenVariableTrans(s)

        self.size2d = s
        self.network_2d = resnet50(k)


    def forward(self, x):
        # d = 2

        # splatted_2d, _ = self.lat_transform(x[:, :3] * (self.size2d//2 - 2), x[:, 3:])
        trans_matrix = self.mat_gen(x)
        trans_matrix = F.normalize(trans_matrix, dim=1)
        splatted_2d, _ = self.lat_transform(x[:, :3] * (self.size2d//2), x[:, 3:], trans_matrix)
        



        # kaidong test
        # import pdb; pdb.set_trace()


        splatted_2d = splatted_2d.permute(0, 3, 1, 2).contiguous()
        outputs = self.network_2d(splatted_2d)

        return outputs, [_[0].permute(0, 3, 1, 2), splatted_2d, _[1]]



class LatticeGenVariableTrans(nn.Module):
    def __init__(self, s):
        super(LatticeGenVariableTrans, self).__init__()

        d = 3
        self.d = d
        # self.d1 = self.d + 1


        self.d1 = self.d
        self.size2d = s

        # canonical
        canonical = torch.arange(self.d1, dtype=torch.long)[None, :].repeat(self.d1, 1)
        # (d+1, d+1)
        for i in range(1, self.d1):
            canonical[-i:, i] = i - self.d1
        self.canonical = canonical.cuda()

        self.dim_indices = torch.arange(self.d1, dtype=torch.long)[:, None].cuda()


    def get_keys_and_barycentric(self, pc):
        """
        :param pc: (self.d, N -- undefined)
        :return:
        """
        batch_size = pc.size(0)
        num_points = pc.size(-1)
        point_indices = torch.arange(num_points, dtype=torch.long)[None, None, :]
        batch_indices = torch.arange(batch_size, dtype=torch.long)[:, None, None]

        # elevated = torch.matmul(self.elevate_mat, pc) * self.expected_std  # (d+1, N)
        elevated = torch.matmul(self.elevate_mat, pc) # * self.expected_std  # (d+1, N)

        # kaidong: to 2d
        # elevated = elevated[:, :self.d1, :] # * self.expected_std  # (d+1, N)

        # kaidong TODO
        ### if FloatTensor: round sometimes rounds to wrong integer
        ### using DoubleTensor to have more precise rounding, and convert back to have better result
        # it's rounding correctly
        # find 0-remainder
        greedy = (torch.round(elevated / self.d1) * self.d1)  # (d+1, N)

        # greedy = elevated // self.d1 * self.d1
        # el_minus_gr = elevated - greedy
        # greedy[el_minus_gr > (self.d1/2)] += self.d1
        # greedy[el_minus_gr < (-self.d1/2)] -= self.d1

        el_minus_gr = elevated - greedy


        rank = torch.sort(el_minus_gr, dim=1, descending=True)[1]
        # the following advanced indexing is different in PyTorch 0.4.0 and 1.0.0
        #rank[rank, point_indices] = self.dim_indices  # works in PyTorch 0.4.0 but fail in PyTorch 1.x
        index = rank.clone()

        rank[batch_indices, index, point_indices] = self.dim_indices  # works both in PyTorch 1.x(has tested in PyTorch 1.2) and PyTorch 0.4.0
        del index


        remainder_sum = greedy.sum(dim=1, keepdim=True) / self.d1

        rank_float = rank.type(torch.float32)
        cond_mask = ((rank_float >= self.d1 - remainder_sum) * (remainder_sum > 0) + \
                     (rank_float < -remainder_sum) * (remainder_sum < 0)) \
            .type(torch.float32)
        sum_gt_zero_mask = (remainder_sum > 0).type(torch.float32)
        sum_lt_zero_mask = (remainder_sum < 0).type(torch.float32)
        sign_mask = -1 * sum_gt_zero_mask + sum_lt_zero_mask

        greedy += self.d1 * sign_mask * cond_mask
        rank += (self.d1 * sign_mask * cond_mask).type_as(rank)
        rank += remainder_sum.type(torch.long)

        # barycentric
        el_minus_gr = elevated - greedy
        greedy = greedy.type(torch.long)

        barycentric = torch.zeros((batch_size, self.d1 + 1, num_points), dtype=torch.float32).cuda()

        # if 3 < rank.max() or rank.max() < 0:
        #     # kaidong test
        #     import pdb; pdb.set_trace()
        # import pdb; pdb.set_trace()

        barycentric[batch_indices, self.d - rank, point_indices] += el_minus_gr
        barycentric[batch_indices, self.d1 - rank, point_indices] -= el_minus_gr
        barycentric /= self.d1
        barycentric[batch_indices, 0, point_indices] += 1. + barycentric[batch_indices, self.d1, point_indices]
        barycentric = barycentric[:, :-1, :]


        # canonical[rank, :]: [d1, num_pts, d1]
        #                     (d1 dim coordinates) then (d1 vertices of a simplex) 
        keys = greedy[:, :, :, None] + self.canonical[rank, :]  # (d1, num_points, d1)
        # rank: rearrange the coordinates of the canonical



        del elevated, greedy, rank, remainder_sum, rank_float, \
            cond_mask, sum_gt_zero_mask, sum_lt_zero_mask, sign_mask
        return keys, barycentric, el_minus_gr

    # def get_filter_size(self, radius):
    #     return (radius + 1) ** self.d1 - radius ** self.d1

    def forward(self, pc1, features, trans_mat):

        self.elevate_mat = trans_mat
        # self.elevate_mat = (torch.FloatTensor([[2, -1, -1], [-1, 2, -1], [-1, -1, 2]]) / torch.tensor(6.).sqrt() ).cuda()

        # keys, bary, el_minus_gr = self.get_single(pc1[0])
        keys, in_barycentric, _ = self.get_keys_and_barycentric(pc1)

        d = 2


        batch_size = features.size(0)
        num_pts = features.size(-1)
        batch_indices = torch.arange(batch_size, dtype=torch.long)

        batch_indices = batch_indices.pin_memory()

        # convert to 2d image
        # coord [3, d * num_pts]: [d] + [d] + ... + [d]
        coord = keys[:, :d].view(batch_size, d, -1)
        offset = coord.min(dim=2)[0]

        coord -= offset.view(batch_size, -1, 1).expand(batch_size, -1, self.d1*num_pts)

        # tmp: [batch, d, d, num]
        # d: coordinates of points; then d: vertices of each simplex
        tmp = in_barycentric[:, None, :, :] * features[:, :, None, :]
        # tmp: [d, d * num]
        # d * num_pts: [d] + [d] + ... + [d]
        tmp = tmp.permute(0, 1, 3, 2).contiguous().view(batch_size, 3, -1).permute(0, 2, 1)

        splatted_2d = torch.zeros((batch_size, self.size2d, self.size2d, 3), dtype=torch.float32).cuda()
        filter_2d = torch.zeros((batch_size, self.size2d//self.d1, self.size2d//self.d1, 3), dtype=torch.float32).cuda()
        pts_pick = (-offset) % 3


        # kaidong mod
        # remove points that are out of range in 2d image
        idx_out_range = coord >= self.size2d
        coord[idx_out_range] = 0
        idx_out_range = idx_out_range.sum(1).nonzero()
        tmp[idx_out_range[:, 0], idx_out_range[:, 1]] = 0


        for i in range(batch_size):
            # coord: [d, d * num]
            # d: d-coordinate; d * num: vertices of simplex * number of points
            # in_barycentric: [batch, d, num]
            # d: 0- 1- 2- 3-... remainder points
            # splatted = sparse_sum(coord, in_barycentric.view(-1), 
            #                       None, args.DEVICE == 'cuda')

            splatted_2d[i] = torch.cuda.sparse.FloatTensor(coord[i], tmp[i], 
                                  torch.Size([self.size2d, self.size2d, 3])).to_dense()

            # splatted_2d[i] = sparse_sum(coord[i], tmp[i], 
            #                       torch.Size([self.size2d, self.size2d, 3]), True)
            filter_2d[i] = splatted_2d[i, pts_pick[i, 0]::self.d1, pts_pick[i, 1]::self.d1][:self.size2d//self.d1, :self.size2d//self.d1]

        # return splatted_2d, filter_2d
        return filter_2d, [splatted_2d, keys.view(batch_size, 3, -1)]

    # def __repr__(self):
    #     format_string = self.__class__.__name__ + '\n(scales_filter_map: {}\n'.format(self.scales_filter_map)
    #     format_string += ')'
    #     return format_string




class LatticeGen(nn.Module):
    def __init__(self, s):
        super(LatticeGen, self).__init__()

        d = 3
        self.d = d
        # self.d1 = self.d + 1


        self.d1 = self.d
        self.size2d = s
        # self.scales_filter_map = args.scales_filter_map

        # elevate_left = torch.ones((self.d1, self.d), dtype=torch.float32).triu()
        # elevate_left[1:, ] += torch.diag(torch.arange(-1, -d - 1, -1, dtype=torch.float32))
        # elevate_right = torch.diag(1. / (torch.arange(1, d + 1, dtype=torch.float32) *
        #                                  torch.arange(2, d + 2, dtype=torch.float32)).sqrt())
        # self.expected_std = (d + 1) * math.sqrt(2 / 3)
        # self.elevate_mat = torch.mm(elevate_left, elevate_right).cuda()
        # (d+1,d)
        # del elevate_left, elevate_right


        self.elevate_mat = (torch.FloatTensor([[2, -1, -1], [-1, 2, -1], [-1, -1, 2]]) / torch.tensor(6.).sqrt() ).cuda()



        # kaidong: transfer to 2d
        # d = 1
        # self.d = d
        # self.d1 = self.d + 1

        # canonical
        canonical = torch.arange(self.d1, dtype=torch.long)[None, :].repeat(self.d1, 1)
        # (d+1, d+1)
        for i in range(1, self.d1):
            canonical[-i:, i] = i - self.d1
        self.canonical = canonical.cuda()

        self.dim_indices = torch.arange(self.d1, dtype=torch.long)[:, None].cuda()

        # self.radius2offset = {}
        # radius_set = set([item for line in self.scales_filter_map for item in line[1:] if item != -1])

        # for radius in radius_set:
        #     hash_table = []
        #     center = np.array([0] * self.d1, dtype=np.long)

        #     traversal = Traverse(radius, self.d)
        #     traversal.go(center, hash_table)
        #     self.radius2offset[radius] = np.vstack(hash_table)

    def get_keys_and_barycentric(self, pc):
        """
        :param pc: (self.d, N -- undefined)
        :return:
        """
        batch_size = pc.size(0)
        num_points = pc.size(-1)
        point_indices = torch.arange(num_points, dtype=torch.long)[None, None, :]
        batch_indices = torch.arange(batch_size, dtype=torch.long)[:, None, None]

        # elevated = torch.matmul(self.elevate_mat, pc) * self.expected_std  # (d+1, N)
        elevated = torch.matmul(self.elevate_mat, pc) # * self.expected_std  # (d+1, N)

        # kaidong: to 2d
        # elevated = elevated[:, :self.d1, :] # * self.expected_std  # (d+1, N)

        # kaidong TODO
        ### if FloatTensor: round sometimes rounds to wrong integer
        ### using DoubleTensor to have more precise rounding, and convert back to have better result
        # it's rounding correctly
        # find 0-remainder
        greedy = (torch.round(elevated / self.d1) * self.d1)  # (d+1, N)

        # greedy = elevated // self.d1 * self.d1
        # el_minus_gr = elevated - greedy
        # greedy[el_minus_gr > (self.d1/2)] += self.d1
        # greedy[el_minus_gr < (-self.d1/2)] -= self.d1

        el_minus_gr = elevated - greedy


        rank = torch.sort(el_minus_gr, dim=1, descending=True)[1]
        # the following advanced indexing is different in PyTorch 0.4.0 and 1.0.0
        #rank[rank, point_indices] = self.dim_indices  # works in PyTorch 0.4.0 but fail in PyTorch 1.x
        index = rank.clone()

        rank[batch_indices, index, point_indices] = self.dim_indices  # works both in PyTorch 1.x(has tested in PyTorch 1.2) and PyTorch 0.4.0
        del index


        remainder_sum = greedy.sum(dim=1, keepdim=True) / self.d1

        rank_float = rank.type(torch.float32)
        cond_mask = ((rank_float >= self.d1 - remainder_sum) * (remainder_sum > 0) + \
                     (rank_float < -remainder_sum) * (remainder_sum < 0)) \
            .type(torch.float32)
        sum_gt_zero_mask = (remainder_sum > 0).type(torch.float32)
        sum_lt_zero_mask = (remainder_sum < 0).type(torch.float32)
        sign_mask = -1 * sum_gt_zero_mask + sum_lt_zero_mask

        greedy += self.d1 * sign_mask * cond_mask
        rank += (self.d1 * sign_mask * cond_mask).type_as(rank)
        rank += remainder_sum.type(torch.long)

        # barycentric
        el_minus_gr = elevated - greedy
        greedy = greedy.type(torch.long)

        barycentric = torch.zeros((batch_size, self.d1 + 1, num_points), dtype=torch.float32).cuda()

        barycentric[batch_indices, self.d - rank, point_indices] += el_minus_gr
        barycentric[batch_indices, self.d1 - rank, point_indices] -= el_minus_gr
        barycentric /= self.d1
        barycentric[batch_indices, 0, point_indices] += 1. + barycentric[batch_indices, self.d1, point_indices]
        barycentric = barycentric[:, :-1, :]


        # canonical[rank, :]: [d1, num_pts, d1]
        #                     (d1 dim coordinates) then (d1 vertices of a simplex) 
        keys = greedy[:, :, :, None] + self.canonical[rank, :]  # (d1, num_points, d1)
        # rank: rearrange the coordinates of the canonical



        del elevated, greedy, rank, remainder_sum, rank_float, \
            cond_mask, sum_gt_zero_mask, sum_lt_zero_mask, sign_mask
        return keys, barycentric, el_minus_gr

    # def get_filter_size(self, radius):
    #     return (radius + 1) ** self.d1 - radius ** self.d1

    def forward(self, pc1, features):
        with torch.no_grad():

            # keys, bary, el_minus_gr = self.get_single(pc1[0])
            keys, in_barycentric, _ = self.get_keys_and_barycentric(pc1)

            d = 2


            batch_size = features.size(0)
            num_pts = features.size(-1)
            batch_indices = torch.arange(batch_size, dtype=torch.long)

            batch_indices = batch_indices.pin_memory()

            # convert to 2d image
            # coord [3, d * num_pts]: [d] + [d] + ... + [d]
            coord = keys[:, :d].view(batch_size, d, -1)
            offset = coord.min(dim=2)[0]


            coord -= offset.view(batch_size, -1, 1).expand(batch_size, -1, self.d1*num_pts)

            # tmp: [batch, d, d, num]
            # d: coordinates of points; then d: vertices of each simplex
            tmp = in_barycentric[:, None, :, :] * features[:, :, None, :]
            # tmp: [d, d * num]
            # d * num_pts: [d] + [d] + ... + [d]
            tmp = tmp.permute(0, 1, 3, 2).contiguous().view(batch_size, 3, -1).permute(0, 2, 1)

            splatted_2d = torch.zeros((batch_size, self.size2d, self.size2d, 3), dtype=torch.float32).cuda()
            filter_2d = torch.zeros((batch_size, self.size2d//self.d1, self.size2d//self.d1, 3), dtype=torch.float32).cuda()
            pts_pick = (-offset) % 3



            for i in range(batch_size):
                # coord: [d, d * num]
                # d: d-coordinate; d * num: vertices of simplex * number of points
                # in_barycentric: [batch, d, num]
                # d: 0- 1- 2- 3-... remainder points
                # splatted = sparse_sum(coord, in_barycentric.view(-1), 
                #                       None, args.DEVICE == 'cuda')

                splatted_2d[i] = sparse_sum(coord[i], tmp[i], 
                                      torch.Size([self.size2d, self.size2d, 3]), True)
                filter_2d[i] = splatted_2d[i, pts_pick[i, 0]::self.d1, pts_pick[i, 1]::self.d1][:self.size2d//self.d1, :self.size2d//self.d1]


        return filter_2d, filter_2d

    # def __repr__(self):
    #     format_string = self.__class__.__name__ + '\n(scales_filter_map: {}\n'.format(self.scales_filter_map)
    #     format_string += ')'
    #     return format_string

