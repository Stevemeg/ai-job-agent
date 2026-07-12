import json
import os


# -----------------------------------
# SAVE JOBS
# -----------------------------------
def save_jobs(jobs):

    os.makedirs(
        "../data",
        exist_ok=True
    )

    with open(
        "../data/jobs.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            jobs,
            file,
            indent=4,
            ensure_ascii=False
        )


# -----------------------------------
# LOAD JOBS
# -----------------------------------
def load_jobs():

    path = "../data/jobs.json"

    if not os.path.exists(path):
        return []

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as file:

        jobs = json.load(file)

    return jobs