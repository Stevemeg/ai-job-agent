import requests

from .job_parser import parse_job


# -----------------------------------
# FETCH REMOTEOK JOBS
# -----------------------------------
def fetch_remoteok_jobs():

    url = "https://remoteok.com/api"

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:

        response = requests.get(
            url,
            headers=headers
        )

        jobs_data = response.json()

        structured_jobs = []

        for job in jobs_data[1:]:

            raw_job = {
                "title": job.get("position"),
                "company": job.get("company"),
                "location": job.get("location"),
                "tags": job.get("tags", []),
                "description": job.get("description"),
                "apply_link": job.get("url")
            }

            parsed_job = parse_job(raw_job)

            structured_jobs.append(parsed_job)

        return structured_jobs

    except Exception as e:

        print(f"Error fetching jobs: {e}")

        return []