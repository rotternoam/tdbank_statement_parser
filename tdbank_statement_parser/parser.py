import re
import sys
from collections import defaultdict
from hashlib import md5
from pathlib import Path

try:
    import pdftotext
except ImportError:
    pdftotext = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from pydash import py_

from .account_statement import parse_config as account_parse_config
from .credit_card_statement import parse_config as credit_card_parse_config

table_cutoff = re.compile(
    r"(^Subtotal\s*\:\s+[\d\,\.]*\s*$|"
    r"^\s+Subtotal\:\s+[\d\,\.]*\s*$|"
    r"Call 1-800-937-2000 for 24-hour|"
    r"TOTAL \w+ FOR THIS PERIOD\s*[$0-9.]+$|"
    r"is based on a full calendar year and does not)",
    re.I
)

daily_balance_header = re.compile(r"^DAILY\s*BALANCE\s*SUMMARY", re.I)
daily_balance_row = re.compile(r"(?P<date>\d{2}\/\d{2})\s+(?P<balance>[\d,\.]+)")


def parse_lines(parse_config: dict, lines: list) -> dict:
    """With the given configuration, parse tables and metadata from the list of text lines.

    Args:
        parse_config (dict): Defines how to extract elements (see ./*_statement.py).
        lines (list): The PDF text contents as a list of strings.

    Returns:
        dict: Structured statement activity tables.
    """
    n = 0
    config = None
    in_daily_balance = False
    data = defaultdict(list)
    while n < len(lines):
        line = lines[n]
        line_stripped = line.strip()

        if not line_stripped:
            config = None
            in_daily_balance = False
            n += 1
            continue

        if daily_balance_header.search(line_stripped):
            config = None
            in_daily_balance = True
            n += 1
            continue

        if in_daily_balance:
            if table_cutoff.search(line) or re.search(r"^DATE\s+BALANCE\s+DATE\s+BALANCE", line, re.I):
                n += 1
                continue
            for m in daily_balance_row.finditer(line):
                data["Daily Balances"].append(m.groupdict())
            n += 1
            continue

        if config:
            if table_cutoff.search(line):
                config = None
                n += 1
                continue
            else:
                if m := config["table_row"].search(line):
                    row = m.groupdict()
                    row["activity_type"] = config["table_name"]  # Store table name
                    data[config["table_name"]].append(row)
                else:
                    # Check if this line is a new section header (should stop this config)
                    # Check if it matches another table name pattern
                    next_config = None
                    for table_cfg in parse_config["tables"].values():
                        if re.search(table_cfg["table_name_re"], line_stripped, flags=re.I):
                            next_config = table_cfg
                            break

                    if next_config and next_config != config:
                        # This is a new section, stop current config
                        config = next_config
                        # Check if next line is the table start
                        if n + 1 < len(lines) and config["table_start"].search(lines[n + 1]):
                            n += config.get("n_lines_after_header", 2)
                        n += 1
                        continue

                    # Only append to description if it looks like a continuation
                    if (
                        data[config["table_name"]]
                        and "description" in data[config["table_name"]][-1]
                        and len(line_stripped) < 100
                        and not (line_stripped.isupper() and len(line_stripped.split()) <= 3)
                    ):
                        data[config["table_name"]][-1]["description"] = (
                            "\n".join(
                                [
                                    data[config["table_name"]][-1]["description"],
                                    line_stripped,
                                ]
                            )
                        )
        if (
            found_config := py_(parse_config["tables"].values())
            .filter(lambda x: re.search(x["table_name_re"], line_stripped, flags=re.I))
            .head()
            .value()
        ):
            config = found_config
            if "Interest Charge Calculation" in config.get("table_name", ""):
                w = 3
            if n + 1 < len(lines) and config["table_start"].search(lines[n + 1]):
                n += config.get("n_lines_after_header", 2)
                n += 1
                continue
        n += 1
    return data


def get_content_type_config(text: str) -> dict:
    """
    Discern a content type based on the first page of the statement.

    Returns the parser configuration for a checking/savings account or a credit card account.
    """
    if re.search(r"STATEMENT\s*OF\s*ACCOUNT", text, re.I):
        return account_parse_config
    elif re.search(r"Please\s+make\s+check.*money\s+order.*payable.*TD\s+Bank.*N\.A\.", text, re.I | re.DOTALL):
        return credit_card_parse_config
    raise Exception("Cannot discern content-type of the file.")


def parse(filepath: str) -> dict:
    """Parse a TD Bank statement into logical parts.

    Args:
        filepath (str): The file path to a TD Bank credit card or account statement.

    Returns:
        dict: {
            file_md5: str,
            filename: str,
            nPages: int,
            metadata: dict[str -> any],
            activity: dict[str -> [dict]]   # Tabular data defined in parse_config.tables
        }
    """
    # Use pdftotext if available, otherwise fall back to pdfplumber, then PyPDF2
    if pdftotext:
        parsed_pdf = list(pdftotext.PDF(open(filepath, "rb"), physical=True))
    elif pdfplumber:
        # Use pdfplumber for better text extraction
        with pdfplumber.open(filepath) as pdf:
            parsed_pdf = [page.extract_text() for page in pdf.pages]
    else:
        # Fall back to PyPDF2 as last resort
        from PyPDF2 import PdfReader
        with open(filepath, "rb") as f:
            pdf_reader = PdfReader(f)
            parsed_pdf = [page.extract_text() for page in pdf_reader.pages]
    content_parse_config = get_content_type_config(parsed_pdf[0])

    normalize = content_parse_config.get("normalize", py_.identity)

    metadata = {}
    if metadata_patterns := content_parse_config.get("metadata_patterns"):
        metadata = {
            k: normalize_value(v)
            for rgx, normalize_value in metadata_patterns
            for m in [re.search(rgx, parsed_pdf[0], flags=re.I | re.M)]
            if m
            for k, v in m.groupdict().items()
        }

    activity_tables = dict(
        parse_lines(
            content_parse_config,
            list(line for page in parsed_pdf for line in page.splitlines()),
        )
    )

    activity = {
        table_name: list(
            map(lambda x: normalize(x, table_name, metadata), records_list)
        )
        for table_name, records_list in activity_tables.items()
    }

    if daily_balances := activity.get("Daily Balances"):
        daily_by_date = {
            row["date"]: row["balance"]
            for row in daily_balances
            if row.get("date") is not None and row.get("balance") is not None
        }
        for table_name, records in activity.items():
            if table_name == "Daily Balances":
                continue
            for row in records:
                if row.get("posting_date") in daily_by_date:
                    row["daily_balance"] = daily_by_date[row["posting_date"]]

    return {
        "file_md5": md5(open(filepath, "rb").read()).hexdigest(),
        "filename": Path(filepath).name,
        "nPages": len(parsed_pdf),
        "metadata": metadata,
        "activity": activity,
    }
