import os
import pathlib

import neptune
import wandb
from rich import print
from rich.traceback import install

from tali.ctools import get_max_supported_batch_size
from tali.data.data import dataclass_collate
from tali.data.data_plus import CustomConcatDataset
from tali.models import TALIModel
from tali.utils import create_hf_model_repo_and_download_maybe

os.environ[
    "HYDRA_FULL_ERROR"
] = "1"  # Makes sure that stack traces produced by hydra instantiation functions produce
# traceback errors related to the modules they built, rather than generic instantiate related errors that
# are generally useless for debugging

os.environ[
    "TORCH_DISTRIBUTED_DEBUG"
] = "DETAIL"  # extremely useful when debugging DDP setups

install()  # beautiful and clean tracebacks for debugging


from typing import List, Optional

import hydra
import torch
from hydra_zen import instantiate
from omegaconf import OmegaConf
from torch.utils.data import Dataset, Subset
import accelerate

from tali.boilerplate import Learner
from tali.callbacks import Callback
from tali.config import BaseConfig, collect_config_store
from tali.evaluators import ClassificationEvaluator
from tali.trainers import ClassificationTrainer
from tali.utils import get_logger, pretty_config, set_seed

config_store = collect_config_store()

logger = get_logger(name=__name__)

accelerator = accelerate.Accelerator()


def instantiate_callbacks(callback_dict: dict) -> List[Callback]:
    callbacks = []
    for cb_conf in callback_dict.values():
        callbacks.append(instantiate(cb_conf))

    return callbacks


@hydra.main(config_path=None, config_name="config", version_base=None)
def run(cfg: BaseConfig) -> None:
    ckpt_path, repo_url = create_hf_model_repo_and_download_maybe(cfg)

    if ckpt_path is not None:
        logger.info(
            f"ckpt_path: {ckpt_path}, exists: {ckpt_path.exists()}, resume: {cfg.resume}, not resume: {not cfg.resume}"
        )
    else:
        logger.info(
            f"ckpt_path: {ckpt_path}, resume: {cfg.resume}, not resume: {not cfg.resume}"
        )

    logger.info(f"Using checkpoint: {ckpt_path}")

    print(pretty_config(cfg, resolve=True))

    set_seed(seed=cfg.seed)

    model: TALIModel = instantiate(cfg.model)
    model = accelerator.prepare(model)

    dummy_model = instantiate(cfg.model)
    dummy_model = accelerator.prepare(dummy_model)

    if ckpt_path is not None and cfg.resume is True:
        trainer_state = torch.load(
            pathlib.Path(ckpt_path) / "trainer_state.pt"
        )
        global_step = trainer_state["global_step"]
        neptune_id = (
            trainer_state["neptune_id"]
            if "neptune_id" in trainer_state
            else None
        )
        experiment_tracker = neptune.init_run(
            source_files=["tali/*.py", "kubernetes/*.py"],
            with_id=neptune_id,
        )
    else:
        global_step = 0
        experiment_tracker = neptune.init_run(
            source_files=["tali/*.py", "kubernetes/*.py"]
        )

    wandb.init()
    config_dict = OmegaConf.to_container(cfg, resolve=True)
    experiment_tracker["config"] = config_dict
    experiment_tracker["notes"] = repo_url
    experiment_tracker["init_global_step"] = global_step

    wandb.config.update(config_dict)
    wandb.config.update({"notes": repo_url})
    wandb.config.update({"init_global_step": global_step})

    train_datasets = []
    val_datasets = []
    test_datasets = []

    for dataset_name, (batch_size, dataset) in cfg.dataset.items():
        logger.info(f"Setting up {dataset_name} train dataset")

        train_dataset: Dataset = instantiate(
            dataset,
            set_name="train",
            total_num_samples=10000000,
            num_samples_per_episode=batch_size,
        )

        val_dataset: Dataset = instantiate(
            dataset,
            set_name="val",
            total_num_samples=1000,
            num_samples_per_episode=batch_size,
        )

        test_dataset: Dataset = instantiate(
            dataset,
            set_name="test",
            total_num_samples=None,
            num_samples_per_episode=batch_size,
        )

        train_datasets.append(train_dataset)
        val_datasets.append(val_dataset)
        test_datasets.append(test_dataset)

    train_dataset = CustomConcatDataset(train_datasets)

    if global_step > 0:
        train_dataset = Subset(
            train_dataset, range(global_step, len(train_dataset))
        )

    train_dataloader = instantiate(
        cfg.dataloader,
        dataset=train_dataset,
        batch_size=1,
        shuffle=True,
        collate_fn=dataclass_collate,
    )

    val_dataset = CustomConcatDataset(val_datasets)

    val_dataloader = instantiate(
        cfg.dataloader,
        dataset=val_dataset,
        batch_size=1,
        shuffle=False,
        collate_fn=dataclass_collate,
    )

    test_dataset = CustomConcatDataset(val_datasets)

    test_dataloader = instantiate(
        cfg.dataloader,
        dataset=test_dataset,
        batch_size=1,
        shuffle=False,
        collate_fn=dataclass_collate,
    )

    experiment_tracker["num_parameters"] = sum(
        p.numel() for p in model.parameters() if p.requires_grad
    )

    optimizer: torch.optim.Optimizer = instantiate(
        cfg.optimizer, params=model.parameters(), _partial_=False
    )

    dummy_optimizer: torch.optim.Optimizer = instantiate(
        cfg.optimizer, params=dummy_model.parameters(), _partial_=False
    )

    scheduler: Optional[torch.optim.lr_scheduler._LRScheduler] = instantiate(
        cfg.scheduler,
        optimizer=optimizer,
        t_initial=cfg.learner.train_iters,
        _partial_=False,
    )

    dummy_scheduler: Optional[
        torch.optim.lr_scheduler._LRScheduler
    ] = instantiate(
        cfg.scheduler,
        optimizer=dummy_optimizer,
        t_initial=cfg.learner.train_iters,
        _partial_=False,
    )

    learner: Learner = instantiate(
        cfg.learner,
        model=model,
        dummy_model=dummy_model,
        trainer=ClassificationTrainer(
            optimizer=optimizer,
            dummy_optimizer=dummy_optimizer,
            scheduler=scheduler,
            dummy_scheduler=dummy_scheduler,
            experiment_tracker=experiment_tracker,
            gradient_clipping=cfg.gradient_clipping,
        ),
        evaluator=ClassificationEvaluator(
            experiment_tracker=experiment_tracker
        ),
        train_dataloader=train_dataloader,
        val_dataloader=val_dataloader,
        callbacks=instantiate_callbacks(cfg.callbacks),
        resume=ckpt_path,
        experiment_tracker=experiment_tracker,
    )

    if cfg.train:
        learner.train()

    if cfg.test:
        learner.test(test_dataloader=test_dataloader)


if __name__ == "__main__":
    run()
