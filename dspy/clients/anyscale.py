from typing import Any, Dict, List, Optional, Union
import json
import yaml
import os
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
import functools

from dspy.utils.logging import logger
from dspy.clients.finetune import (
    FinetuneJob,
    TrainingMethod,
    save_data,
)
from dspy.clients.openai import openai_data_validation, check_message_lengths
import asyncio

try:
    # Importing the AnyScale library for users in the AnyScale workspace, where
    # the library is already installed.
    from anyscale.job import JobConfig
    import anyscale
except ImportError:
    anyscale = None

#-------------------------------------------------------------------------------
#    Variables
#-------------------------------------------------------------------------------

# List of training methods supported by AnyScale
TRAINING_METHODS_ANYSCALE = [
    TrainingMethod.SFT,
]

PROVIDER_ANYSCALE = "anyscale"


#-------------------------------------------------------------------------------
#    Launching and killing LMs
#-------------------------------------------------------------------------------

def anyscale_model_launch(model: str, launch_kwargs: Dict[str, Any]):
    """Launch an AnyScale model."""
    # TODO: Hardcode resources for launching a select server through docker
    raise NotImplementedError("Method `anyscale_model_launch` is not implemented.")


def anyscale_model_kill(model: str, launch_kwargs: Dict[str, Any]):
    # TODO: Hardcode resources for killing a select server through docker
    """Kill an AnyScale model."""
    raise NotImplementedError("Method `anyscale_model_kill` is not implemented.")


#-------------------------------------------------------------------------------
#    Function and classes required for the fine-tune interface
#-------------------------------------------------------------------------------


def is_anyscale_model(model: str) -> bool:
    """Check if the model is an AnyScale model."""
    # TODO: This needs to be implemented to support fine-tuning
    logger.info("Is AnyScale model is not implemented, returning False as a default to not break lm.py")
    return False


class FinetuneJobAnyScale(FinetuneJob):

    def __init__(self, *args, **kwargs):
        self.job_id = None
        super().__init__(*args, **kwargs)
    
    def cancel(self):
        """Cancel the finetune job."""
        raise NotImplementedError("Method `cancel` is not implemented.")
        # Call the super's cancel method after the custom cancellation logic, 
        # so that the future can be cancelled
        # super().cancel()

    def status(self):
        """Get the status of the finetune job."""
        raise NotImplementedError("Method `status` is not implemented.")


def finetune_anyscale(
        job: FinetuneJobAnyScale,
        model: str,
        train_data: List[Dict[str, Any]],
        train_kwargs: Optional[Dict[str, Any]]=None,
    ) -> Union[str, Exception]:
    """Start the finetune job."""
    try:
        train_kwargs = train_kwargs or {}
        assert "model" not in train_kwargs, "Model should not be in the train_kwargs"
        train_kwargs["model"] = model
        train_method = TrainingMethod.SFT  # Note: This could be an argument

        logger.info("[Finetune] Starting training process...")
        if train_method not in TRAINING_METHODS_ANYSCALE:
            raise NotImplementedError(f"AnyScale can only support {TRAINING_METHODS_ANYSCALE} for the time being")
        
        logger.info("[Finetune] Retrieving the `serve_config_path` if provided, otherwise defaulting to `serve_1B.yaml`...")
        serve_config_path = train_kwargs.pop("serve_config_path", "serve_1B.yaml")

        logger.info("[Finetune] Validating the dataset format...")
        if not verify_dataset(train_data):
            # TODO: Does AnyScale support text completion models?
            err = "[Finetune] Error: Unable to verify that the dataset is in the correct format."
            logger.error(err)
            raise RuntimeError(err)

        logger.info("[Finetune] Converting data to JSONL format...")
        data_path = save_data(train_data, provider_name=PROVIDER_ANYSCALE)

        logger.info("[Finetune] Submitting data to remote storage...")
        remote_train_path, _ = submit_data(train_path=data_path, eval_path=None)
        logger.info(f"[Finetune] Data submitted. Remote train path: {remote_train_path}")

        logger.info("[Finetune] Generating configuration files...")
        _, compute_config = generate_config_files(train_path=remote_train_path, eval_path=None, **train_kwargs)
        
        logger.info("[Finetune] Starting remote training...")
        job_id = start_remote_training(compute_config=compute_config, **train_kwargs)
        job.job_id = job_id
        logger.info(f"[Finetune] Remote training started. Job ID: {job.job_id}")

        logger.info("[Finetune] Waiting for training to complete...")
        wait_for_training(job.job_id)
        logger.info("[Finetune] Training completed.")

        logger.info("[Finetune] Retrieving model information...")
        model_info = get_model_info(job.job_id)
        logger.info(f"[Finetune] Model info retrieved: {model_info}")

        storage_uri = model_info["storage_uri"]
        logger.info(f"[Finetune] Copying LoRA weights from {storage_uri}...")
        model_names, lora_dynamic_path = copy_lora_weights(storage_uri, model_info, job.job_id)
        logger.info(f"[Finetune] LoRA weights copied. Model names: {model_names}")

        logger.info("[Finetune] Setting result in future object...")
        last_model_checkpoint = model_names[-1]  # TODO: Is this correct?
        logger.info("[Finetune] Training process completed successfully.")

        # TODO: Is this the right place to call update_model_config?
        logger.info("[Finetune] Updating model config with the proper dynamic path")
        update_model_config(lora_dynamic_path, serve_config_path)

        return last_model_checkpoint

    except Exception as e:
        logger.error(f"[Finetune] Error occurred during training: {str(e)}")
        raise e


#-------------------------------------------------------------------------------
#    Custom functions to support the finetune_* function
#-------------------------------------------------------------------------------

def wait_for_training(job_id):
    """Wait for the training to complete."""
    anyscale.job.wait(id=job_id, timeout_s=18000)


def update_model_config(lora_dynamic_path: str, serve_config_path: str):
    """Update the model config storage location with the job_id."""
    with open(serve_config_path, "r") as f:
        serve_config = yaml.safe_load(f)
    
    model_config_location = serve_config["applications"][0]["args"]["llm_configs"][0]
    
    with open(model_config_location, "r") as f:
        model_config = yaml.safe_load(f)

    model_config["lora_config"]["dynamic_lora_loading_path"] = lora_dynamic_path
    
    with open(model_config_location, "w") as f:
        yaml.safe_dump(model_config, f)
    

def verify_dataset(dataset: List[dict[str, Any]]) -> bool:
    """Verify the training arguments before starting training."""
    dataset_validation = openai_data_validation(dataset)

    if dataset_validation:
        logger.error(f"Dataset validation failed: {dataset_validation}")
        return False

    # TODO: Is this still useful?
    convo_lens = check_message_lengths(dataset)
    return True


def submit_data(train_path: str, eval_path: Optional[str]):
    """Upload the data to the Workspace cloud storage."""
    storage = os.environ['ANYSCALE_ARTIFACT_STORAGE']
    
    datasets = {"train": train_path}
    if eval_path:
        datasets["val"] = eval_path

    fine_tuning_file_ids = {}
    for name, path in datasets.items():
        num_items = len(read_jsonl(path))
        logger.info(f"Number of items in {name} data: {num_items}")

        remote_path = os.path.join(storage, path.split("/")[-1])
        logger.info(f"Uploading {name} data to S3 at {remote_path}")
        if remote_path[:2] == "s3":
            os.system(f"aws s3 cp {path} {remote_path}")
        elif remote_path[:2] == "gs":
            os.system(f"gcloud storage cp {path} {remote_path}")
        else:
            os.system(f"cp {path} {remote_path}")
            logger.info(f"Copied {path} to {remote_path}")
        fine_tuning_file_ids[name] = remote_path
    
    return fine_tuning_file_ids["train"], fine_tuning_file_ids.get("val", None)


def generate_config_files(train_path: str, eval_path: Optional[str], **kwargs):
    base_model_yaml_path = kwargs.get("train_config_yaml", None)
    assert kwargs["model"] is not None, "Model is required to generate the config files"

    use_lora = kwargs.get("use_lora", False)
    example_dir = ""
    lora_path = "configs/training/lora" if use_lora else "configs/training/full_param"


    if not base_model_yaml_path:
        def get_yaml_config(model_name):
            if "llama" in model_name.lower():
                if "70b" in model_name:
                    return "llama-3-70b.yaml"
                elif "13b" in model_name:
                    return "llama-3-70b.yaml"
                else:
                    return "llama-3-8b.yaml"
            elif "mistral" in model_name.lower():
                if "mixtral" in model_name.lower():
                    return "mixtral-8x7b.yaml"
                else:
                    return "mistral-7b.yaml"
            else:
                raise RuntimeError("No default yaml found for the model")

        default_model_yaml_path = get_yaml_config(kwargs["model"])
        base_model_yaml_path = os.path.join(example_dir, lora_path, default_model_yaml_path)
        logger.info(f"Using default yaml template for model: {base_model_yaml_path}")
        
    model_config_data = yaml.safe_load(open(base_model_yaml_path, "r"))
    model_config_data.update(kwargs.get("hyperparameters", {}))
    
    model_config_data["model_id"] = kwargs["model"]

    custom_modifications = {
        "model_id": kwargs["model"],
        "train_path": train_path,
        "valid_path": eval_path,
        "logger": {
            "provider": "wandb",
        },
        "num_checkpoints_to_keep": 10
    }
    if kwargs.get("output_dir", None):
        custom_modifications["output_dir"] = kwargs["output_dir"]

    model_config_data.update(custom_modifications)
    model_config_data = {k: v for k, v in model_config_data.items() if v is not None}

    def freeze(d):
        if isinstance(d, dict):
            return tuple(sorted((key, freeze(value)) for key, value in d.items()))
        elif isinstance(d, list):
            return tuple(freeze(value) for value in sorted(d))
        elif isinstance(d, set):
            return tuple(freeze(value) for value in sorted(d))
        return d

    def hash_dict(d):
        return hash(freeze(d))
    dict_sorted_hash = hash_dict(model_config_data)
    if dict_sorted_hash < 0:
        dict_sorted_hash = -dict_sorted_hash
    filename = f"model_config_dspy_{dict_sorted_hash}.yaml"
    logger.info(f"Model config data: {model_config_data}")
    yaml.safe_dump(model_config_data, open(filename, "w"))

    ft_path = os.path.join("utils", "ft.py")

    compute_config_dict = {
        "name": "dspy-llmforge-fine-tuning-job",
        "entrypoint": f"llmforge anyscale finetune {filename}",
        "working_dir": ".",
        "image_uri": "localhost:5555/anyscale/llm-forge:0.5.6",
        "requirements": [
            "wandb",
        ],
        "env_vars": {
            "WANDB_API_KEY": os.environ.get("WANDB_API_KEY", ""),
            "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
            "HF_HOME": os.environ.get("HF_HOME", ""),
        }
    }
    compute_config_kwargs = kwargs.get("compute_config", {})
    compute_config_dict.update(compute_config_kwargs)
    compute_config = JobConfig(**compute_config_dict)

    job_runner_config_path = kwargs.get("compute_yaml_path", "job_runner_config.yaml")

    return job_runner_config_path, compute_config


def start_remote_training(compute_config, **kwargs) -> str:
    job_id: str = anyscale.job.submit(compute_config)
    return job_id


def wait_for_training(job):
    logger.info("Waiting for training to complete")
    anyscale.job.wait(id=job.job_id, timeout_s=18000)


def get_model_info(job_id):
    return anyscale.llm.model.get(job_id=job_id).to_dict()


def copy_lora_weights(storage_uri, model_info, job_id):
    try:
        from google.cloud import storage

        storage_client = storage.Client()

        bucket_name = storage_uri.split('/')[2]
        source_folder = '/'.join(storage_uri.split('/')[3:-1])
        logger.info(f"Source folder: {source_folder}")

        bucket = storage_client.bucket(bucket_name)

        blobs = bucket.list_blobs(prefix=source_folder)

        subfolders = set()
        for blob in blobs:
            if '/' in blob.name[len(source_folder):]:
                subfolder = blob.name.split('/')[:-1]
                subfolders.add('/'.join(subfolder))

        base_model_id = model_info["base_model_id"]
        lora_dynamic_path = f"dspy/lora_weights/{job_id}/{base_model_id}"

        model_names = []
        for subfolder in subfolders:
            subfolder_name = subfolder.split('/')[-1]
            destination_folder = f"{lora_dynamic_path}:{subfolder_name}"
            if subfolder_name.startswith("epoch"):
                model_names.append("/".join(destination_folder.split("/")[-2:]))
            else:
                continue
            
            subfolder_blobs = bucket.list_blobs(prefix=subfolder)
            
            for blob in subfolder_blobs:
                source_blob = bucket.blob(blob.name)
                destination_blob_name = f"{destination_folder}/{blob.name.split('/')[-1]}"
                bucket.copy_blob(source_blob, bucket, destination_blob_name)
                logger.info(f"Copied {source_blob.name} to {destination_blob_name}")

        logger.info(f"All subfolders copied to: gs://{bucket_name}/{lora_dynamic_path}")
        completed_path = f"gs://{bucket_name}/{lora_dynamic_path}"
        return model_names, completed_path
    
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        raise e


def read_jsonl(filename):
    with open(filename, "r") as f:
        return [json.loads(line) for line in f]


# TODO: Is it true that we don't need this function anymore?
def write_jsonl(filename, data):
    with open(filename, "w") as f:
        for item in data:
            f.write(json.dumps(item) + "\n")