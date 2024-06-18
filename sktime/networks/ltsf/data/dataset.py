import os
import warnings

import numpy as np
import pandas as pd
import torch
from sklearn.preprocessing import StandardScaler
from torch.utils.data import DataLoader, Dataset

from sktime.networks.ltsf.utils.timefeatures import time_features


class Dataset_Custom(Dataset):
    def __init__(
        self,
        X,
        y,
        flag="train",
        size=None,
        features="S",
        target="OT",
        scale=True,
        timeenc=0,
        freq="h",
        train_only=False,
    ):
        # size [seq_len, label_len, pred_len]
        # info
        if size == None:
            self.seq_len = 24 * 4 * 4
            self.label_len = 24 * 4
            self.pred_len = 24 * 4
        else:
            self.seq_len = size[0]
            self.label_len = size[1]
            self.pred_len = size[2]
            # size = [6, 3, 2]
            # [1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1]
            # seq [1, 2, 3, 4, 5, 6], label [7, 8, 9], pred [0, 1]
            # x [1, 2, 3, 4, 5, 6], y[7, 8, 9, 0, 0]
        # init
        assert flag in ["train", "test", "val"]
        type_map = {"train": 0, "val": 1, "test": 2}
        self.set_type = type_map[flag]

        self.features = features
        self.target = target
        self.scale = scale
        self.timeenc = timeenc
        self.freq = freq
        self.train_only = train_only

        self.X = X
        self.y = y

        self.__read_data__()

    def create_df(self):
        """
        this function converts self.X and self.y to DataFrame of
        ['date', ...(other features), target feature]
        """
        df = pd.concat([self.X, self.y], axis=1)
        df.index = df.index.to_timestamp()
        df.index.rename("date", inplace=True)
        df.reset_index(inplace=True)
        df.rename(columns={"index": "date"}, inplace=True)
        self.target = df.columns[-1]

        return df

    def __read_data__(self):
        self.scaler = StandardScaler()
        # df_raw = pd.read_csv(os.path.join(self.root_path,
        # 								self.data_path))
        df_raw = self.create_df()

        """
		df_raw.columns: ['date', ...(other features), target feature]
		"""
        cols = list(df_raw.columns)
        if self.features == "S":
            cols.remove(self.target)
        cols.remove("date")
        # print(cols)
        num_train = int(len(df_raw) * (0.7 if not self.train_only else 1))
        num_test = int(len(df_raw) * 0.2)
        num_vali = len(df_raw) - num_train - num_test
        # border1s = [0, num_train - self.seq_len, len(df_raw) - num_test - self.seq_len + 1]
        # border2s = [num_train, num_train + num_vali, len(df_raw) + 1]
        border1s = [
            0,
            num_train - self.seq_len,
            len(df_raw) - self.seq_len - self.pred_len,
        ]
        border2s = [num_train, num_train + num_vali, len(df_raw)]
        border1 = border1s[self.set_type]
        border2 = border2s[self.set_type]

        if self.features == "M" or self.features == "MS":
            df_raw = df_raw[["date"] + cols]
            cols_data = df_raw.columns[1:]
            df_data = df_raw[cols_data]
        elif self.features == "S":
            df_raw = df_raw[["date"] + cols + [self.target]]
            df_data = df_raw[[self.target]]

        if self.scale:
            train_data = df_data[border1s[0] : border2s[0]]
            self.scaler.fit(train_data.values)
            # print(self.scaler.mean_)
            # exit()
            data = self.scaler.transform(df_data.values)
        else:
            data = df_data.values

        df_stamp = df_raw[["date"]][border1:border2]
        df_stamp["date"] = pd.to_datetime(df_stamp.date)
        if self.timeenc == 0:
            df_stamp["month"] = df_stamp.date.apply(lambda row: row.month, 1)
            df_stamp["day"] = df_stamp.date.apply(lambda row: row.day, 1)
            df_stamp["weekday"] = df_stamp.date.apply(lambda row: row.weekday(), 1)
            df_stamp["hour"] = df_stamp.date.apply(lambda row: row.hour, 1)
            data_stamp = df_stamp.drop(["date"], axis=1).values
        elif self.timeenc == 1:
            data_stamp = time_features(
                pd.to_datetime(df_stamp["date"].values), freq=self.freq
            )
            data_stamp = data_stamp.transpose(1, 0)

        self.data_x = data[border1:border2]
        self.data_y = data[border1:border2]
        self.data_stamp = data_stamp

    def __getitem__(self, index):
        s_begin = index
        s_end = s_begin + self.seq_len
        r_begin = s_end - self.label_len
        r_end = r_begin + self.label_len + self.pred_len

        seq_x = torch.tensor(self.data_x[s_begin:s_end]).float()
        seq_y = torch.tensor(self.data_y[r_begin:r_end]).float()
        seq_x_mark = torch.tensor(self.data_stamp[s_begin:s_end]).float()
        seq_y_mark = torch.tensor(self.data_stamp[r_begin:r_end]).float()

        dec_inp = torch.zeros_like(seq_y[-self.pred_len :, :])
        dec_inp = torch.cat([seq_y[: self.label_len, :], dec_inp], dim=0)

        return (
            {
                "x_enc": seq_x,
                "x_mark_enc": seq_x_mark,
                "x_dec": dec_inp,
                "x_mark_dec": seq_y_mark,
            },
            seq_y[-self.pred_len :],
        )

    def __len__(self):
        return len(self.data_x) - self.seq_len - self.pred_len + 1

    def inverse_transform(self, data):
        return self.scaler.inverse_transform(data)
