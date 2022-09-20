import logging
from typing import Optional
import numpy as np
import tensorflow as tf
import pydantic_argparse
from rich.console import Console
from sklearn.metrics import f1_score
from . import datasets as ds
from .metrics import confusion_matrix_plot, roc_auc_plot
from .types import EcgTask, EcgTestParams
from .utils import setup_logger, set_random_seed

console = Console()
logger = logging.getLogger('ecgarr')

@tf.function
def parallelize_dataset(
        db_path: str,
        patient_ids: int = None,
        task: EcgTask = EcgTask.rhythm,
        frame_size: int = 1250,
        samples_per_patient: int = 100,
        repeat: bool = False,
        num_workers: int = 1
):
    """ Generates datasets for given task in parallel using TF `interleave`

    Args:
        db_path (str): Database path
        patient_ids (int, optional): List of patient IDs. Defaults to None.
        task (EcgTask, optional): ECG Task routine. Defaults to EcgTask.rhythm.
        frame_size (int, optional): Frame size. Defaults to 1250.
        samples_per_patient (int, optional): # Samples per pateint. Defaults to 100.
        repeat (bool, optional): Should data generator repeat. Defaults to False.
        num_workers (int, optional): Number of parallel workers. Defaults to 1.
    """
    def _make_train_dataset(i, split):
        return ds.create_dataset_from_generator(
            task=task, db_path=db_path,
            patient_ids=patient_ids[i * split:(i + 1) * split], frame_size=frame_size,
            samples_per_patient=samples_per_patient, repeat=repeat
        )
    split = len(patient_ids) // num_workers
    datasets = [_make_train_dataset(i, split) for i in range(num_workers)]
    par_ds = tf.data.Dataset.from_tensor_slices(datasets)
    return par_ds.interleave(lambda x: x, cycle_length=num_workers, num_parallel_calls=tf.data.experimental.AUTOTUNE)


def load_test_dataset(
        db_path: str,
        task: str = 'rhythm',
        frame_size: int = 1250,
        test_patients: Optional[float] = None,
        test_pt_samples: Optional[int] = None,
        num_workers: int = 1
    ):
    """ Load testing datasets
    Args:
        db_path (str): Database path
        task (EcgTask, optional): ECG Task. Defaults to EcgTask.rhythm.
        frame_size (int, optional): Frame size. Defaults to 1250.
        train_patients (Optional[float], optional): # or proportion of train patients. Defaults to None.
        val_patients (Optional[float], optional): # or proportion of train patients. Defaults to None.
        train_pt_samples (Optional[int], optional): # samples per patient for training. Defaults to None.
        val_pt_samples (Optional[int], optional): # samples per patient for training. Defaults to None.
        train_file (Optional[str], optional): Path to existing picked training file. Defaults to None.
        val_file (Optional[str], optional): Path to existing picked validation file. Defaults to None.
        num_workers (int, optional): # of parallel workers. Defaults to 1.

    Returns:
        Tuple[tf.data.Dataset, tf.data.Dataset]: Training and validation datasets
    """
    test_patient_ids = ds.icentia11k.get_test_patient_ids()
    if test_patients is not None:
        num_pts = int(test_patients) if test_patients > 1 else int(test_patients*len(test_patient_ids))
        test_patient_ids = test_patient_ids[:num_pts]

    test_size = len(test_patient_ids) * test_pt_samples * 4
    logger.info(f'Collecting {test_size} test samples')
    test_patient_ids = tf.convert_to_tensor(test_patient_ids)
    test_data = parallelize_dataset(
        db_path=db_path, patient_ids=test_patient_ids, task=task, frame_size=frame_size,
        samples_per_patient=test_pt_samples, repeat=False, num_workers=num_workers
    )
    with console.status("[bold green] Loading test dataset..."):
        test_x, test_y = next(test_data.batch(test_size).as_numpy_iterator())
        test_data = ds.create_dataset_from_data(test_x, test_y, task=task, frame_size=frame_size)
    return test_data

def evaluate_model(params: EcgTestParams):
    """ Test model command. This evaluates a trained network on the given ECG task and dataset.

    Args:
        params (EcgTestParams): Testing/evaluation parameters
    """
    params.seed = set_random_seed(params.seed)
    logger.info(f'Random seed {params.seed}')

    logger.info("Loading test dataset")
    test_data = load_test_dataset(
        db_path=str(params.db_path),
        task=params.task,
        frame_size=params.frame_size,
        test_patients=params.test_patients,
        test_pt_samples=params.samples_per_patient,
        num_workers=params.data_parallelism
    )
    test_data = test_data.batch(
        batch_size=512,
        drop_remainder=True,
        num_parallel_calls=tf.data.experimental.AUTOTUNE
    )
    strategy = tf.distribute.MirroredStrategy()
    with strategy.scope():
        logger.info("Loading model")
        model = tf.keras.models.load_model(params.model_file)
        model.summary()

        logger.info("Performing inference")
        test_labels = []
        for _, label in test_data:
            test_labels.append(label.numpy())
        y_true = np.concatenate(test_labels)
        y_prob = model.predict(test_data)
        y_pred = np.argmax(y_prob, axis=1)

        # Summarize results
        logger.info("Testing Results")
        class_names = ds.get_class_names(params.task)
        test_acc = np.sum(y_pred == y_true) / len(y_true)
        test_f1 = f1_score(y_true, y_pred, average='macro')
        logger.info(f"TEST SET: ACC={test_acc:.2%}, F1={test_f1:.2%}")
        confusion_matrix_plot(y_true, y_pred, labels=class_names, save_path=str(params.job_dir / 'confusion_matrix_test.png'))
        if len(class_names) == 2:
            y_prob1 = tf.nn.softmax(y_prob)[:,1].numpy()
            roc_auc_plot(y_true, y_prob1, labels=class_names, save_path=str(params.job_dir / 'roc_auc_test.png'))

def create_parser():
    """ Create CLI argument parser
    Returns:
        ArgumentParser: Arg parser
    """
    return pydantic_argparse.ArgumentParser(
        model=EcgTestParams,
        prog="ECG Arrhythmia Test Command",
        description="Test ECG arrhythmia model"
    )

if __name__ == '__main__':
    """ Run ecgarr.test as CLI. """
    setup_logger('ecgarr')
    parser = create_parser()
    args = parser.parse_typed_args()
    evaluate_model(args)