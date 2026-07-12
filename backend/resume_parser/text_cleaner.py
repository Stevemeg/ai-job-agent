import re


def clean_resume_text(text):

    # Preserve line structure
    text = re.sub(r'[ \t]+', ' ', text)

    # Remove excessive blank lines
    text = re.sub(r'\n+', '\n', text)

    return text.strip()