import json
import pathlib

import pyarrow as pa
from datasets import Dataset
from rich import print

train_data_dir = pathlib.Path(
    "/data/generator/default-e3d897e3cfea555e/0.0.0/"
)
print(f"Loading data from {train_data_dir}")
arrow_files = []

for file_name in train_data_dir.iterdir():
    if not file_name.name.endswith(".arrow"):
        continue
    print(file_name)
    arrow_files.append(file_name)

arrow_tables = []

for arrow_file in arrow_files:
    print(f"Loading {arrow_file}")
    arrow_tables.append(pa.ipc.open_file(arrow_file).read_all())

concatenated_table = pa.concat_tables(arrow_tables)

train_data = Dataset.from_parquet(concatenated_table)

dataset_info = json.load(open(f"{train_data_dir}/dataset_info.json", "r"))

train_data.info = dataset_info
