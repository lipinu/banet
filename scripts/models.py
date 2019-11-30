from fastai.vision import *

__all__ = ['BA_Net']

DROP = 0.2

class BTNorm2d(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.bn = nn.BatchNorm2d(channels)
        
    def forward(self, x):
        n_sequences, ch, sequence_len, sz1, sz2 = x.size()
        x = x.permute(0,2,1,3,4).contiguous().view(n_sequences*sequence_len, ch, sz1, sz2)
        x = self.bn(x)
        x = x.view(n_sequences, sequence_len, ch, sz1, sz2).permute(0,2,1,3,4).contiguous()
        return x


class LSTM(nn.Module):
    def __init__(self, ni, nf):
        super().__init__()
        self.lstm = nn.LSTM(ni, nf, num_layers=1, bidirectional=False, batch_first=True)
        
    def forward(self, x):
        bs, ch, ts, sz1, sz2 = x.size()
        x = x.permute(0, 3, 4, 2, 1).contiguous().view(bs*sz1*sz2, ts, ch)
        x, (h, c) = self.lstm(x)
        x = x.view(bs, sz1, sz2, ts, ch).permute(0, 4, 3, 1, 2).contiguous()
        return x, h


class SpaceConv(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_sz, stride):
        super().__init__()
        spaceConv = nn.Conv3d(in_ch, out_ch, kernel_size=(1, kernel_sz, kernel_sz),
                              stride=(1, stride, stride),
                              padding=(0, kernel_sz//2, kernel_sz//2), bias=False)
        layers = [spaceConv, BTNorm2d(out_ch), nn.ReLU(inplace=True)]
        self.conv = nn.Sequential(*layers)
    def forward(self, x): return self.conv(x)

    
class UpSpaceConv(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        upConv = nn.ConvTranspose3d(in_ch, out_ch, kernel_size=(1, 2, 2), stride=(1, 2, 2), bias=False)
        layers = [upConv, BTNorm2d(out_ch), nn.ReLU(inplace=True)]
        self.conv = nn.Sequential(*layers)
    def forward(self, x): return self.conv(x)

        
class TimeConv(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_sz, stride, use_lstm):
        super().__init__()
        self.use_lstm = use_lstm
        self.timeConv = nn.Conv3d(out_ch, out_ch, kernel_size=(kernel_sz, 1, 1),
                            stride=(stride, 1, 1),
                            padding=(kernel_sz//2, 0, 0), bias=False)
        if self.use_lstm: self.timeLstm = LSTM(out_ch, out_ch)
        self.bn = BTNorm2d(out_ch)
        self.relu = nn.ReLU(inplace=True)
    def forward(self, x): 
        if self.use_lstm:
            x, _ = self.timeLstm(x)
        x = self.timeConv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x

    
class UpTimeConv(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        upTimeConv = nn.ConvTranspose3d(in_ch, out_ch, kernel_size=(2, 1, 1), stride=(2, 1, 1), bias=False)
        layers = [upTimeConv, BTNorm2d(out_ch), nn.ReLU(inplace=True)]
        self.conv = nn.Sequential(*layers)
    def forward(self, x): return self.conv(x)
        
    
class SpaceTimeConv(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_sz, time_sz, stride, time_stride, time_ch, use_lstm=False):
        super().__init__()
        spaceConv = SpaceConv(in_ch, out_ch, kernel_sz, stride)
        timeConv = TimeConv(out_ch, out_ch, time_sz, time_stride, use_lstm)
        layers = [spaceConv, timeConv, nn.Dropout3d(DROP)] 
        self.stconv = nn.Sequential(*layers)
    def forward(self, x): return self.stconv(x)


class UpSpaceTimeConv(nn.Module):
    def __init__(self, in_ch, out_ch, time_ch):
        super().__init__()
        upSpaceConv = UpSpaceConv(in_ch, out_ch)
        upTimeConv = UpTimeConv(out_ch, out_ch)
        layers = [upSpaceConv, upTimeConv, nn.Dropout3d(DROP)] 
        self.upstconv = nn.Sequential(*layers)
    def forward(self, x): return self.upstconv(x) 


class BA_Net(nn.Module):
    def __init__(self, in_ch, n_classes, sequence_len):
        super().__init__()
        n=1
        self.stconv1 = SpaceTimeConv(in_ch, n*32, 7, 7, 1, 1, sequence_len, use_lstm=True)  
        self.stconv2 = SpaceTimeConv(n*32, n*64, 3, 7, 2, 2, sequence_len//2)  
        self.stconv3 = SpaceTimeConv(n*64, n*128, 3, 5, 2, 2, sequence_len//4) 
        self.stconv4 = SpaceTimeConv(n*128,n*256, 3, 3, 2, 2, sequence_len//8) 
        self.stconv5 = SpaceTimeConv(n*256,n*256, 3, 3, 2, 2, sequence_len//16)
        self.stconv6 = SpaceConv(n*256, n*256, 3, 2)
        self.ustconv6 = UpSpaceConv(n*256, n*256)
        self.ustconv5 = UpSpaceTimeConv(n*512, n*256, sequence_len//8)
        self.ustconv4 = UpSpaceTimeConv(n*512, n*128, sequence_len//4)
        self.ustconv3 = UpSpaceTimeConv(n*256, n*64, sequence_len//2)
        self.ustconv2 = UpSpaceTimeConv(n*128, n*32, sequence_len)
        self.conv3 = nn.Conv3d(n*64, n*64, kernel_size=(3,1,1), padding=(3//2, 0, 0), bias=False)
        self.bn = BTNorm2d(n*64)
        self.relu = nn.ReLU(inplace=True)
        self.final_conv = nn.Conv3d(n*64, n_classes, kernel_size=1)
        
    def forward(self, x):
        c1 = self.stconv1(x)
        c2 = self.stconv2(c1)
        c3 = self.stconv3(c2)
        c4 = self.stconv4(c3)
        c5 = self.stconv5(c4)
        c6 = self.stconv6(c5)
        up = self.ustconv6(c6)
        up = torch.cat((c5, up), dim=1)
        up = self.ustconv5(up)
        up = torch.cat((c4, up), dim=1)
        up = self.ustconv4(up)
        up = torch.cat((c3, up), dim=1)
        up = self.ustconv3(up)
        up = torch.cat((c2, up), dim=1)
        up = self.ustconv2(up)
        up = torch.cat((c1, up), dim=1)
        up = self.final_conv(self.relu(self.bn(self.conv3(up))))
        return up