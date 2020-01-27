"""
Dataset for clip model
"""
import logging
import torch
from torch.utils.data import Dataset
import numpy as np
import h5py
from utils.basic_utils import load_jsonl, load_json, l2_normalize_np_array, flat_list_of_lists, merge_dicts
from utils.tensor_utils import pad_sequences_1d

logger = logging.getLogger(__name__)


class RetrievalDataset(Dataset):
    """
    Args:
        dset_name, str, ["tvr"]
        ctx_mode: str,
    Return:
        a dict: {
            "meta": {
                "desc_id": int,
                "desc": str,
                "vid_name": str,
                "duration": float,
                "ts": [st (float), ed (float)], seconds, ground_truth timestamps
            }
            "model_inputs": {
                "query_feat": torch.tensor, (L, D_q)
                "video_feat": torch.tensor, (n_clip_in_moment, D_video)
                "sub_feat": torch.tensor, (n_clip_in_moment, D_sub)
                "st_ed_indices": torch.LongTensor, (2, )
            }
        }
    """
    def __init__(self, dset_name, data_path, desc_bert_path_or_handler, sub_bert_path_or_handler,
                 vid_feat_path_or_handler, max_desc_len, max_ctx_len, ctx_mode="video",
                 normalize_vfeat=True, normalize_tfeat=True, h5driver=None, data_ratio=1.0):
        self.dset_name = dset_name
        self.data_path = data_path
        self.data_ratio = data_ratio
        self.max_desc_len = max_desc_len
        self.max_ctx_len = max_ctx_len

        self.desc_bert_path_or_handler = desc_bert_path_or_handler
        self.sub_bert_path_or_handler = sub_bert_path_or_handler
        self.vid_feat_path_or_handler = vid_feat_path_or_handler
        self.ctx_mode = ctx_mode

        # prepare desc data
        self.data = load_jsonl(data_path)
        if self.data_ratio != 1:
            n_examples = int(len(self.data) * data_ratio)
            self.data = self.data[:n_examples]
            logger.info("Using {}% of the data: {} examples".format(data_ratio * 100, n_examples))

        self.use_video = "video" in self.ctx_mode
        self.use_sub = "sub" in self.ctx_mode
        self.use_tef = "tef" in self.ctx_mode

        if self.use_video:
            if isinstance(vid_feat_path_or_handler, h5py.File):
                self.vid_feat_h5 = vid_feat_path_or_handler
            else:  # str path
                self.vid_feat_h5 = h5py.File(vid_feat_path_or_handler, "r", driver=h5driver)

        if isinstance(desc_bert_path_or_handler, h5py.File):
            self.desc_bert_h5 = desc_bert_path_or_handler
        else:
            self.desc_bert_h5 = h5py.File(desc_bert_path_or_handler, "r", driver=h5driver)

        if self.use_sub:
            if isinstance(sub_bert_path_or_handler, h5py.File):
                self.sub_bert_h5 = sub_bert_path_or_handler
            else:  # str path
                self.sub_bert_h5 = h5py.File(sub_bert_path_or_handler, "r", driver=h5driver)

        self.normalize_vfeat = normalize_vfeat
        self.normalize_tfeat = normalize_tfeat

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        raw_data = self.data[index]

        # initialize with basic data
        meta = dict(
            desc_id=raw_data["desc_id"],
            desc=raw_data["desc"],
            vid_name=raw_data["vid_name"],
            duration=raw_data["duration"],
        )
        model_inputs = dict()
        model_inputs["query_feat"] = self.get_query_feat_by_desc_id(meta["desc_id"])

        ctx_l = 0
        if self.use_video:
            video_feat = np.mean(self.vid_feat_h5[meta["vid_name"]][:self.max_ctx_len], axis=0)  # (D, )
            if self.normalize_vfeat:
                video_feat = l2_normalize_np_array(video_feat)
            model_inputs["video_feat"] = torch.from_numpy(video_feat)
        else:
            model_inputs["video_feat"] = torch.zeros(2)

        if self.use_sub:  # no need for ctx feature, as the features are already contextulized
            sub_feat = np.mean(self.sub_bert_h5[meta["vid_name"]][:self.max_ctx_len], axis=0)  # (N_clips, D_t)
            if self.normalize_tfeat:
                sub_feat = l2_normalize_np_array(sub_feat)
            model_inputs["sub_feat"] = torch.from_numpy(sub_feat)
        else:
            model_inputs["sub_feat"] = torch.zeros(2)
        return dict(meta=meta, model_inputs=model_inputs)

    def get_query_feat_by_desc_id(self, desc_id):
        query_feat = self.desc_bert_h5[str(desc_id)][:self.max_desc_len]
        if self.normalize_tfeat:
            query_feat = l2_normalize_np_array(query_feat)
        return torch.from_numpy(query_feat)


class RetrievalEvalDataset(Dataset):
    """
    init_data_mode: `video_query` or `video_only` or `query_only`,
        it indicates which data to load when initialize the Dataset object.
    data_mode: `context` or `query`, it indicates which data to return for self.__get_item__()
    desc_bert_path_or_handler: h5py.File object or str path
    vid_feat_path_or_handler: h5py.File object or str path
    eval_proposal_bsz: the proposals for a single video will be sorted in length and batched here with
        max batch size to be eval_proposal_bsz. A single video might have multiple batches of proposals.
    load_gt_video: load GroundTruth Video, useful when evaluating single video moment retrieval.
    data_ratio: percentage of query data to use.
    """
    def __init__(self, dset_name, eval_split_name, data_path=None,
                 desc_bert_path_or_handler=None, max_desc_len=None,  max_ctx_len=None,
                 sub_bert_path_or_handler=None, vid_feat_path_or_handler=None,
                 video_duration_idx_path=None, ctx_mode="video", data_mode="context",
                 h5driver=None, data_ratio=1.0, normalize_vfeat=True, normalize_tfeat=True):
        self.dset_name = dset_name
        self.eval_split_name = eval_split_name
        self.ctx_mode = ctx_mode
        self.load_gt_video = False
        self.data_ratio = data_ratio  # only affect query data
        self.normalize_vfeat = normalize_vfeat
        self.normalize_tfeat = normalize_tfeat

        self.data_mode = None
        self.set_data_mode(data_mode)

        self.max_desc_len = max_desc_len
        self.max_ctx_len = max_ctx_len
        self.data_path = data_path
        self.query_data = load_jsonl(data_path)
        if data_ratio != 1:
            n_examples = int(len(self.query_data) * data_ratio)
            self.query_data = self.query_data[:n_examples]
            logger.info("Using {}% of the data: {} examples".format(data_ratio * 100, n_examples))
        if isinstance(desc_bert_path_or_handler, h5py.File):
            self.desc_bert_h5 = desc_bert_path_or_handler
        else:
            self.desc_bert_h5 = h5py.File(desc_bert_path_or_handler, "r", driver=h5driver)

        video_data = load_json(video_duration_idx_path)[self.eval_split_name]
        self.video_data = [{"vid_name": k, "duration": v[0]} for k, v in video_data.items()]
        self.video2idx = {k: v[1] for k, v in video_data.items()}

        self.use_video = "video" in self.ctx_mode
        self.use_sub = "sub" in self.ctx_mode
        self.use_tef = "tef" in self.ctx_mode

        if self.use_video:
            if isinstance(vid_feat_path_or_handler, h5py.File):
                self.vid_feat_h5 = vid_feat_path_or_handler
            else:  # str path
                self.vid_feat_h5 = h5py.File(vid_feat_path_or_handler, "r", driver=h5driver)

        if self.use_sub:
            if isinstance(sub_bert_path_or_handler, h5py.File):
                self.sub_bert_h5 = sub_bert_path_or_handler
            else:  # str path
                self.sub_bert_h5 = h5py.File(sub_bert_path_or_handler, "r", driver=h5driver)

    def set_data_mode(self, data_mode):
        """context or query"""
        assert data_mode in ["context", "query"]
        self.data_mode = data_mode

    def load_gt_vid_name_for_query(self, load_gt_video):
        """load_gt_video: bool, affect the returned value of self._get_item_query"""
        assert "vid_name" in self.query_data[0]
        self.load_gt_video = load_gt_video

    def __len__(self):
        if self.data_mode == "context":
            return len(self.video_data)
        else:
            return len(self.query_data)

    def __getitem__(self, index):
        if self.data_mode == "context":
            return self._get_item_context(index)
        else:
            return self._get_item_query(index)

    def get_query_feat_by_desc_id(self, desc_id):
        query_feat = self.desc_bert_h5[str(desc_id)][:self.max_desc_len]
        if self.normalize_tfeat:
            query_feat = l2_normalize_np_array(query_feat)
        return torch.from_numpy(query_feat)

    def _get_item_query(self, index):
        """Need to batch"""
        raw_data = self.query_data[index]

        meta = dict(
            desc_id=raw_data["desc_id"],
            desc=raw_data["desc"],
            vid_name=raw_data["vid_name"] if self.load_gt_video else None
        )

        model_inputs = dict()
        model_inputs["query_feat"] = self.get_query_feat_by_desc_id(meta["desc_id"])
        return dict(meta=meta, model_inputs=model_inputs)

    def _get_item_context(self, index):
        """No need to batch, since it has already been batched here"""
        raw_data = self.video_data[index]

        # initialize with basic data
        meta = dict(
            vid_name=raw_data["vid_name"],
            duration=raw_data["duration"],
        )

        model_inputs = dict()

        if self.use_video:
            video_feat = np.mean(self.vid_feat_h5[meta["vid_name"]][:self.max_ctx_len], axis=0)  # (1, D)
            if self.normalize_vfeat:
                video_feat = l2_normalize_np_array(video_feat)
            model_inputs["video_feat"] = torch.from_numpy(video_feat)
        else:
            model_inputs["video_feat"] = torch.zeros(2)

        if self.use_sub:  # no need for ctx feature, as the features are already contextulized
            sub_feat = np.mean(self.sub_bert_h5[meta["vid_name"]][:self.max_ctx_len], axis=0)
            if self.normalize_tfeat:
                sub_feat = l2_normalize_np_array(sub_feat)
            model_inputs["sub_feat"] = torch.from_numpy(sub_feat)
        else:
            model_inputs["sub_feat"] = torch.zeros(2)
        return dict(meta=meta, model_inputs=model_inputs)


def retrieval_collate(batch):
    batch_meta = [e["meta"] for e in batch]  # seems no need to collate ?

    model_inputs_keys = batch[0]["model_inputs"].keys()
    batched_data = dict()
    for k in model_inputs_keys:
        if k == "query_feat":
            batched_data[k] = pad_sequences_1d(
                [e["model_inputs"][k] for e in batch], dtype=torch.float32, fixed_length=None)
        elif "feat" in k:
            batched_data[k] = torch.stack([e["model_inputs"][k] for e in batch])
    return batch_meta, batched_data


def prepare_batch_inputs(batched_model_inputs, device, non_blocking=False):
    model_inputs = {}
    for k, v in batched_model_inputs.items():
        if k == "query_feat":
            model_inputs[k] = v[0].to(device, non_blocking=non_blocking)
            model_inputs[k.replace("feat", "mask")] = v[1].to(device, non_blocking=non_blocking)
        else:
            model_inputs[k] = v.to(device, non_blocking=non_blocking)
    return model_inputs


if __name__ == '__main__':
    from baselines.crossmodal_moment_localization.config import BaseOptions
    options = BaseOptions().parse()