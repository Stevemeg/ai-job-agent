import re
def extract_name(text):

    lines = text.split('\n')

    for line in lines:

        line = line.strip()

        # Skip empty lines
        if not line:
            continue

        # Return first meaningful line
        return line

    return None
# -----------------------------------
# EMAIL EXTRACTION
# -----------------------------------
def extract_email(text):

    pattern = r'[\w\.-]+@[\w\.-]+\.\w+'

    matches = re.findall(pattern, text)

    return matches[0] if matches else None


# -----------------------------------
# PHONE EXTRACTION
# -----------------------------------
def extract_phone(text):

    pattern = r'(?:\+91[\-\s]?)?[6-9]\d{9}'

    matches = re.findall(pattern, text)

    if matches:
        return matches[0]

    return None