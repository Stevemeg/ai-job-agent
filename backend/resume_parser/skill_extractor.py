import pandas as pd
import re


# -----------------------------------
# LOAD SKILLS DATABASE
# -----------------------------------
def load_skills_database(csv_path):

    return pd.read_csv(csv_path)


# -----------------------------------
# EXTRACT SKILLS
# -----------------------------------
def extract_skills(text, skills_df):

    text = text.lower()

    extracted_skills = []

    seen_skills = set()

    for _, row in skills_df.iterrows():

        skill = str(
            row["skill"]
        ).strip()

        skill_lower = skill.lower()

        pattern = r'\b' + re.escape(
            skill_lower
        ) + r'\b'

        if re.search(
            pattern,
            text
        ):

            if skill_lower not in seen_skills:

                extracted_skills.append({
                    "skill": row["skill"],
                    "category": row["category"],
                    "domain": row["domain"]
                })

                seen_skills.add(
                    skill_lower
                )

    return extracted_skills