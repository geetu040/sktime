import abc

import numpy as np
import pandas as pd

from sktime.forecasting.base import ForecastingHorizon, _BaseGlobalForecaster
from sktime.utils.dependencies import _check_soft_dependencies

if _check_soft_dependencies("torch", severity="none"):
    import torch
    from torch.utils.data import Dataset
else:

    class Dataset:
        """Dummy class if torch is unavailable."""


class BaseDeepNetworkPyTorch(_BaseGlobalForecaster):
    """Abstract base class for deep learning networks using torch.nn."""

    _tags = {
        "python_dependencies": ["torch"],
        "y_inner_mtype": [
            "pd.DataFrame",
            "pd-multiindex",
            "pd_multiindex_hier",
        ],
        "X_inner_mtype": [
            "pd.DataFrame",
            "pd-multiindex",
            "pd_multiindex_hier",
        ],
        "capability:insample": False,
        "capability:pred_int:insample": False,
        "scitype:y": "both",
        "ignores-exogeneous-X": True,
        "capability:global_forecasting": True,  # (for_global)
    }

    def __init__(
        self,
        num_epochs=16,
        batch_size=8,
        in_channels=1,
        individual=False,
        criterion_kwargs=None,
        optimizer=None,
        optimizer_kwargs=None,
        lr=0.001,
    ):
        self.num_epochs = num_epochs
        self.batch_size = batch_size
        self.in_channels = in_channels
        self.individual = individual
        self.criterion_kwargs = criterion_kwargs
        self.optimizer = optimizer
        self.optimizer_kwargs = optimizer_kwargs
        self.lr = lr

        super().__init__()

        # TODO: pytorch device
        # TODO: training verbose
        # TODO: broadcasting

    def _fit(self, y, fh, X=None):
        """Fit the network.

        Changes to state:
            writes to self._network.state_dict

        Parameters
        ----------
        X : iterable-style or map-style dataset
            see (https://pytorch.org/docs/stable/data.html) for more information
        """
        fh = fh.to_relative(self.cutoff)

        self.network = self._build_network(list(fh)[-1])

        self._criterion = self._instantiate_criterion()
        self._optimizer = self._instantiate_optimizer()

        dataloader = self.build_dataloader(y)
        self.network.train()

        for epoch in range(self.num_epochs):
            self._run_epoch(epoch, dataloader)

    def _run_epoch(self, epoch, dataloader):
        for x, y in dataloader:
            y_pred = self.network(x)
            loss = self._criterion(y_pred, y)
            self._optimizer.zero_grad()
            loss.backward()
            self._optimizer.step()

    def _instantiate_optimizer(self):
        if self.optimizer:
            if self.optimizer in self.optimizers.keys():
                if self.optimizer_kwargs:
                    return self.optimizers[self.optimizer](
                        self.network.parameters(), lr=self.lr, **self.optimizer_kwargs
                    )
                else:
                    return self.optimizers[self.optimizer](
                        self.network.parameters(), lr=self.lr
                    )
            else:
                raise TypeError(
                    f"Please pass one of {self.optimizers.keys()} for `optimizer`."
                )
        else:
            # default optimizer
            return torch.optim.Adam(self.network.parameters(), lr=self.lr)

    def _instantiate_criterion(self):
        if self.criterion:
            if self.criterion in self.criterions.keys():
                if self.criterion_kwargs:
                    return self.criterions[self.criterion](**self.criterion_kwargs)
                else:
                    return self.criterions[self.criterion]()
            else:
                raise TypeError(
                    f"Please pass one of {self.criterions.keys()} for `criterion`."
                )
        else:
            # default criterion
            return torch.nn.MSELoss()

    def _predict(self, X=None, fh=None, y=None):
        """Predict with fitted model."""
        from torch import cat

        fh = fh.to_relative(self.cutoff)
        _y = y if self._global_forecasting else self._y

        dataloader = self.build_dataloader(_y, split="test")

        self.network.eval()
        pred = []
        for x, _ in dataloader:
            pred.append(self.network(x).detach())
        pred = cat(pred, dim=0)

        # converting pred datatype

        if isinstance(_y.index, pd.MultiIndex):
            ins = np.array(
                list(np.unique(_y.index.droplevel(-1)).repeat(pred.shape[1]))
            )
            ins = [ins[..., i] for i in range(ins.shape[-1])] if ins.ndim > 1 else [ins]

            idx = (
                ForecastingHorizon(range(1, pred.shape[1] + 1), freq=self.fh.freq)
                .to_absolute(self._cutoff)
                ._values.tolist()
                * pred.shape[0]
            )
            index = pd.MultiIndex.from_arrays(
                ins + [idx],
                names=_y.index.names,
            )
        else:
            index = (
                ForecastingHorizon(range(1, pred.shape[1] + 1))
                .to_absolute(self._cutoff)
                ._values
            )

        pred = pd.DataFrame(
            # batch_size * num_timestams, n_cols
            pred.reshape(-1, pred.shape[-1]),
            index=index,
            columns=_y.columns,
        )

        absolute_horizons = fh.to_absolute_index(self.cutoff)
        dateindex = pred.index.get_level_values(-1).map(
            lambda x: x in absolute_horizons
        )
        pred = pred.loc[dateindex]
        pred.index.names = _y.index.names

        return pred

    def _prepare_data(self, y):
        if not isinstance(y.index, pd.MultiIndex):
            # shape: (n_timestamps, n_cols)
            y = np.expand_dims(y.values, axis=0)
            # self.X = np.expand_dims(X.values, axis=0) if X is not None else None
            # shape: (1, n_timestamps, n_cols)
        else:
            y = _frame2numpy(y)
            # self.X = _frame2numpy(X) if X is not None else X

        return y

    def build_dataloader(self, y, split="train"):
        y = self._prepare_data(y)

        seq_len = self.network.seq_len
        pred_len = self.network.pred_len

        if split == "test":
            # truncate y to keep only relevant history of seq_len
            y = y[:, -seq_len:, :]
            pred_len = 0

        """Build PyTorch DataLoader for training."""
        from torch.utils.data import DataLoader

        if self.custom_dataset_train:
            if hasattr(self.custom_dataset_train, "build_dataset") and callable(
                self.custom_dataset_train.build_dataset
            ):
                self.custom_dataset_train.build_dataset(y)
                dataset = self.custom_dataset_train
            else:
                raise NotImplementedError(
                    "Custom Dataset `build_dataset` method is not available. Please "
                    f"refer to the {self.__class__.__name__}.build_dataset "
                    "documentation."
                )
        else:
            dataset = PyTorchDataset(
                y=y,
                seq_len=seq_len,
                pred_len=pred_len,
            )

        return DataLoader(dataset, self.batch_size, shuffle=True)

    def build_pytorch_train_dataloader(self, y):
        """Build PyTorch DataLoader for training."""
        from torch.utils.data import DataLoader

        if self.custom_dataset_train:
            if hasattr(self.custom_dataset_train, "build_dataset") and callable(
                self.custom_dataset_train.build_dataset
            ):
                self.custom_dataset_train.build_dataset(y)
                dataset = self.custom_dataset_train
            else:
                raise NotImplementedError(
                    "Custom Dataset `build_dataset` method is not available. Please "
                    f"refer to the {self.__class__.__name__}.build_dataset "
                    "documentation."
                )
        else:
            dataset = PyTorchTrainDataset(
                y=y,
                seq_len=self.network.seq_len,
                fh=self._fh.to_relative(self.cutoff)._values[-1],
            )

        return DataLoader(dataset, self.batch_size, shuffle=True)

    def build_pytorch_pred_dataloader(self, y, fh):
        """Build PyTorch DataLoader for prediction."""
        from torch.utils.data import DataLoader

        if self.custom_dataset_pred:
            if hasattr(self.custom_dataset_pred, "build_dataset") and callable(
                self.custom_dataset_pred.build_dataset
            ):
                self.custom_dataset_train.build_dataset(y)
                dataset = self.custom_dataset_train
            else:
                raise NotImplementedError(
                    "Custom Dataset `build_dataset` method is not available. Please"
                    f"refer to the {self.__class__.__name__}.build_dataset"
                    "documentation."
                )
        else:
            dataset = PyTorchPredDataset(
                y=y[-self.network.seq_len :],
                seq_len=self.network.seq_len,
            )

        return DataLoader(
            dataset,
            self.batch_size,
        )

    def get_y_true(self, y):
        """Get y_true values for validation."""
        dataloader = self.build_pytorch_pred_dataloader(y)
        y_true = [y.flatten().numpy() for _, y in dataloader]
        return np.concatenate(y_true, axis=0)

    @abc.abstractmethod
    def _build_network(self, fh):
        pass


class PyTorchDataset(Dataset):
    """Dataset for use in sktime deep learning forecasters."""

    def __init__(
        self,
        y: pd.DataFrame,
        seq_len: int,
        pred_len: int,
    ):
        self.y = y
        self._num, self._len, _ = self.y.shape
        self.seq_len = seq_len
        self.pred_len = pred_len
        self._len_single = self._len - self.seq_len - self.pred_len + 1

    def __len__(self):
        """Return length of dataset."""
        true_length = self._num * max(self._len_single, 0)
        return true_length

    def __getitem__(self, i):
        """Return data point."""
        from torch import tensor

        n = i // self._len_single
        m = i % self._len_single

        hist_y = self.y[n, m : m + self.seq_len]
        futu_y = self.y[n, m + self.seq_len : m + self.seq_len + self.pred_len]

        hist_y = tensor(hist_y).float()
        futu_y = tensor(futu_y).float()

        return hist_y, futu_y


class PyTorchTrainDataset(Dataset):
    """Dataset for use in sktime deep learning forecasters."""

    def __init__(self, y, seq_len, fh=None, X=None):
        self.y = y.values
        self.X = X.values if X is not None else X
        self.seq_len = seq_len
        self.fh = fh

    def __len__(self):
        """Return length of dataset."""
        return max(len(self.y) - self.seq_len - self.fh + 1, 0)

    def __getitem__(self, i):
        """Return data point."""
        from torch import from_numpy, tensor

        hist_y = tensor(self.y[i : i + self.seq_len]).float()
        if self.X is not None:
            exog_data = tensor(
                self.X[i + self.seq_len : i + self.seq_len + self.fh]
            ).float()
        else:
            exog_data = tensor([])
        return (
            torch.cat([hist_y, exog_data]),
            from_numpy(self.y[i + self.seq_len : i + self.seq_len + self.fh]).float(),
        )


class PyTorchPredDataset(Dataset):
    """Dataset for use in sktime deep learning forecasters."""

    def __init__(self, y, seq_len, X=None):
        self.y = y.values
        self.seq_len = seq_len
        self.X = X.values if X is not None else X

    def __len__(self):
        """Return length of dataset."""
        return 1

    def __getitem__(self, i):
        """Return data point."""
        from torch import from_numpy, tensor

        hist_y = tensor(self.y[i : i + self.seq_len]).float()
        if self.X is not None:
            exog_data = tensor(
                self.X[i + self.seq_len : i + self.seq_len + self.fh]
            ).float()
        else:
            exog_data = tensor([])
        return (
            torch.cat([hist_y, exog_data]),
            from_numpy(self.y[i + self.seq_len : i + self.seq_len]).float(),
        )


def _same_index(data):
    data = data.groupby(level=list(range(len(data.index.levels) - 1))).apply(
        lambda x: x.index.get_level_values(-1)
    )
    assert data.map(
        lambda x: x.equals(data.iloc[0])
    ).all(), "All series must has the same index"
    return data.iloc[0], len(data.iloc[0])


def _frame2numpy(data):
    idx, length = _same_index(data)
    arr = np.array(data.values, dtype=np.float32).reshape(
        (-1, length, len(data.columns))
    )
    return arr


def _to_multiindex(data, index_name="h0", instance_name="h0_0"):
    res = pd.DataFrame(
        data.values,
        index=pd.MultiIndex.from_product(
            [[instance_name], data.index], names=[index_name, data.index.name]
        ),
        columns=[data.name] if isinstance(data, pd.Series) else data.columns,
    )
    return res
