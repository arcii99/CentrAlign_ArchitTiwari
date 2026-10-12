"""Document reading: text PDFs come through as text, scans through local OCR with their layout intact."""

from agent.docs import read_pdf
from world import config
from world.scenarios import reset


def test_scans_are_read_by_ocr_with_exact_numbers():
    reset("demo")
    cheque = read_pdf(config.DRIVE / "AP" / "Vendor_Onboarding" / "Initech" / "Cancelled_Cheque.pdf")
    assert cheque["ocr"] and "50200012345687" in cheque["text"]  # the digit that decides T3
    scanned = read_pdf(next(config.WORLD_DIR.glob("mail_files/*/scan_0042.pdf")))
    assert scanned["ocr"] and "SCC-0042" in scanned["text"] and "53,100.00" in scanned["text"]
    text_pdf = read_pdf(next(config.WORLD_DIR.glob("mail_files/*/GLX-2026-0917.pdf")))
    assert not text_pdf["ocr"] and "1,77,000.00" in text_pdf["text"]
