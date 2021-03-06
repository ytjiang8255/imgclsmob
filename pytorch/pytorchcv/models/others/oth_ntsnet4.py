import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from resnet import resnet50b
from common import conv1x1, conv3x3

__all__ = ['ntsnet']


def generate_default_anchor_maps(anchors_setting=None, input_shape=(448, 448)):
    """
    generate default anchor

    :param anchors_setting: all informations of anchors
    :param input_shape: shape of input images, e.g. (h, w)
    :return: center_anchors: # anchors * 4 (oy, ox, h, w)
             edge_anchors: # anchors * 4 (y0, x0, y1, x1)
             anchor_area: # anchors * 1 (area)
    """
    if anchors_setting is None:
        _default_anchors_setting = (
            dict(layer='p3', stride=32, size=48, scale=[2 ** (1. / 3.), 2 ** (2. / 3.)], aspect_ratio=[0.667, 1, 1.5]),
            dict(layer='p4', stride=64, size=96, scale=[2 ** (1. / 3.), 2 ** (2. / 3.)], aspect_ratio=[0.667, 1, 1.5]),
            dict(layer='p5', stride=128, size=192, scale=[1, 2 ** (1. / 3.), 2 ** (2. / 3.)],
                 aspect_ratio=[0.667, 1, 1.5]),
        )
        anchors_setting = _default_anchors_setting

    center_anchors = np.zeros((0, 4), dtype=np.float32)
    edge_anchors = np.zeros((0, 4), dtype=np.float32)
    anchor_areas = np.zeros((0,), dtype=np.float32)
    input_shape = np.array(input_shape, dtype=int)

    for anchor_info in anchors_setting:

        stride = anchor_info['stride']
        size = anchor_info['size']
        scales = anchor_info['scale']
        aspect_ratios = anchor_info['aspect_ratio']

        output_map_shape = np.ceil(input_shape.astype(np.float32) / stride)
        output_map_shape = output_map_shape.astype(np.int)
        output_shape = tuple(output_map_shape) + (4,)
        ostart = stride / 2.
        oy = np.arange(ostart, ostart + stride * output_shape[0], stride)
        oy = oy.reshape(output_shape[0], 1)
        ox = np.arange(ostart, ostart + stride * output_shape[1], stride)
        ox = ox.reshape(1, output_shape[1])
        center_anchor_map_template = np.zeros(output_shape, dtype=np.float32)
        center_anchor_map_template[:, :, 0] = oy
        center_anchor_map_template[:, :, 1] = ox
        for scale in scales:
            for aspect_ratio in aspect_ratios:
                center_anchor_map = center_anchor_map_template.copy()
                center_anchor_map[:, :, 2] = size * scale / float(aspect_ratio) ** 0.5
                center_anchor_map[:, :, 3] = size * scale * float(aspect_ratio) ** 0.5

                edge_anchor_map = np.concatenate((center_anchor_map[..., :2] - center_anchor_map[..., 2:4] / 2.,
                                                  center_anchor_map[..., :2] + center_anchor_map[..., 2:4] / 2.),
                                                 axis=-1)
                anchor_area_map = center_anchor_map[..., 2] * center_anchor_map[..., 3]
                center_anchors = np.concatenate((center_anchors, center_anchor_map.reshape(-1, 4)))
                edge_anchors = np.concatenate((edge_anchors, edge_anchor_map.reshape(-1, 4)))
                anchor_areas = np.concatenate((anchor_areas, anchor_area_map.reshape(-1)))

    return center_anchors, edge_anchors, anchor_areas


def hard_nms(cdds,
             top_n=10,
             iou_thresh=0.25):
    assert (type(cdds) == np.ndarray)
    assert (len(cdds.shape) == 2)
    assert (cdds.shape[1] >= 5)

    cdds = cdds.copy()
    indices = np.argsort(cdds[:, 0])
    cdds = cdds[indices]
    cdd_results = []

    res = cdds

    while res.any():
        cdd = res[-1]
        cdd_results.append(cdd)
        if len(cdd_results) == top_n:
            return np.array(cdd_results)
        res = res[:-1]

        start_max = np.maximum(res[:, 1:3], cdd[1:3])
        end_min = np.minimum(res[:, 3:5], cdd[3:5])
        lengths = end_min - start_max
        intersec_map = lengths[:, 0] * lengths[:, 1]
        intersec_map[np.logical_or(lengths[:, 0] < 0, lengths[:, 1] < 0)] = 0
        iou_map_cur = intersec_map / ((res[:, 3] - res[:, 1]) * (res[:, 4] - res[:, 2]) + (cdd[3] - cdd[1]) * (
            cdd[4] - cdd[2]) - intersec_map)
        res = res[iou_map_cur < iou_thresh]

    return np.array(cdd_results)


class ProposalBlock(nn.Module):
    """
    Proposal block for Proposal network.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    stride : int or tuple/list of 2 int
        Strides of the convolution.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 stride):
        super(ProposalBlock, self).__init__()
        mid_channels = 128

        self.down_conv = conv3x3(
            in_channels=in_channels,
            out_channels=mid_channels,
            stride=stride,
            bias=True)
        self.activ = nn.ReLU(inplace=False)
        self.tidy_conv = conv1x1(
            in_channels=mid_channels,
            out_channels=out_channels,
            bias=True)

    def forward(self, x):
        y = self.down_conv(x)
        y = self.activ(y)
        z = self.tidy_conv(y)
        z = z.view(z.size(0), -1)
        return z, y


class ProposalNet(nn.Module):
    def __init__(self):
        super(ProposalNet, self).__init__()
        self.branch1 = ProposalBlock(
            in_channels=2048,
            out_channels=6,
            stride=1)
        self.branch2 = ProposalBlock(
            in_channels=128,
            out_channels=6,
            stride=2)
        self.branch3 = ProposalBlock(
            in_channels=128,
            out_channels=9,
            stride=2)

    def forward(self, x):
        t1, x = self.branch1(x)
        t2, x = self.branch2(x)
        t3, _ = self.branch3(x)
        return torch.cat((t1, t2, t3), dim=1)


class Flatten(nn.Module):
    """
    Simple flatten module.
    """

    def forward(self, x):
        return x.view(x.size(0), -1)


class NTSNet(nn.Module):
    def __init__(self,
                 backbone,
                 top_n=4,
                 aux=False):
        super(NTSNet, self).__init__()
        self.top_n = top_n
        self.aux = aux
        self.CAT_NUM = 4

        _, edge_anchors, _ = generate_default_anchor_maps()
        self.edge_anchors = (edge_anchors + 224).astype(np.int)
        self.edge_anchors = np.concatenate(
            (self.edge_anchors.copy(), np.arange(0, len(self.edge_anchors)).reshape(-1, 1)), axis=1)

        self.backbone = backbone

        self.backbone_tail = nn.Sequential()
        self.backbone_tail.add_module("final_pool", nn.AdaptiveAvgPool2d(1))
        self.backbone_tail.add_module("flatten", Flatten())
        self.backbone_tail.add_module("dropout", nn.Dropout(p=0.5))
        self.backbone_classifier = nn.Linear(512 * 4, 200)

        pad_side = 224
        self.pad = nn.ZeroPad2d(padding=(pad_side, pad_side, pad_side, pad_side))

        self.proposal_net = ProposalNet()
        self.concat_net = nn.Linear(2048 * (self.CAT_NUM + 1), 200)
        self.partcls_net = nn.Linear(512 * 4, 200)

    def forward(self, x):
        raw_pre_features = self.backbone(x)

        rpn_score = self.proposal_net(raw_pre_features)
        all_cdds = [np.concatenate((y.reshape(-1, 1), self.edge_anchors.copy()), axis=1)
                    for y in rpn_score.detach().numpy()]
        top_n_cdds = [hard_nms(y, top_n=self.top_n, iou_thresh=0.25) for y in all_cdds]
        top_n_cdds = np.array(top_n_cdds)
        top_n_index = top_n_cdds[:, :, -1].astype(np.int)
        top_n_index = torch.from_numpy(top_n_index).long().to(x.device)
        top_n_prob = torch.gather(rpn_score, dim=1, index=top_n_index)

        batch = x.size(0)
        part_imgs = torch.zeros([batch, self.top_n, 3, 224, 224]).to(x.device)
        x_pad = self.pad(x)
        for i in range(batch):
            for j in range(self.top_n):
                [y0, x0, y1, x1] = top_n_cdds[i][j, 1:5].astype(np.int)
                part_imgs[i:i + 1, j] = F.interpolate(
                    x_pad[i:i + 1, :, y0:y1, x0:x1],
                    size=(224, 224),
                    mode="bilinear",
                    align_corners=True)
        part_imgs = part_imgs.view(batch * self.top_n, 3, 224, 224)
        part_features = self.backbone_tail(self.backbone(part_imgs.detach()))

        part_features = part_features.view(batch, self.top_n, -1)
        part_features = part_features[:, :self.CAT_NUM, ...].contiguous()
        part_features = part_features.view(batch, -1)

        raw_features = self.backbone_tail(raw_pre_features.detach())

        # concat_logits have the shape: B*200
        concat_out = torch.cat([part_features, raw_features], dim=1)
        concat_logits = self.concat_net(concat_out)

        if self.aux:
            raw_logits = self.backbone_classifier(raw_features)

            # part_logits have the shape: B*N*200
            part_logits = self.partcls_net(part_features).view(batch, self.top_n, -1)

            return concat_logits, raw_logits, part_logits, top_n_prob
        else:
            return concat_logits


def ntsnet(pretrained=False, pretrained_backbone=False, aux=True):
    backbone = resnet50b(pretrained=pretrained_backbone).features
    del backbone[-1]
    return NTSNet(backbone=backbone, aux=aux)


def _calc_width(net):
    import numpy as np
    net_params = filter(lambda p: p.requires_grad, net.parameters())
    weight_count = 0
    for param in net_params:
        weight_count += np.prod(param.size())
    return weight_count


def _test():
    import torch
    from torch.autograd import Variable

    pretrained = False
    aux = False

    models = [
        ntsnet,
    ]

    for model in models:

        net = model(pretrained=pretrained, aux=aux)

        # net.train()
        net.eval()
        weight_count = _calc_width(net)
        print("m={}, {}".format(model.__name__, weight_count))
        assert (model != ntsnet or weight_count == 29033133)

        x = Variable(torch.randn(2, 3, 448, 448))
        ys = net(x)
        y = ys[0] if aux else ys
        y.sum().backward()
        assert (tuple(y.size()) == (2, 200))


if __name__ == "__main__":
    _test()
