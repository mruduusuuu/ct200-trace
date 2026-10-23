import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")
DATA = Path(__file__).resolve().parent.parent / "data"
V1 = str(DATA / "ct200_manual.pdf")
V2 = str(DATA / "ct200_manual_v2.pdf")


@pytest.fixture(scope="session")
def doc1():
    from app.parser import parse_pdf
    return parse_pdf(V1)


@pytest.fixture(scope="session")
def doc2():
    from app.parser import parse_pdf
    return parse_pdf(V2)
