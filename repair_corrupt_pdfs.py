"""Redownload known corrupt NSE PDFs and replace only when the new copy validates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from utils import NSE_BASE_URL, NSE_HEADERS, request_with_retries


REPAIR_TARGETS = {
    Path("downloads/NSE/Zuari_Agro_Chemicals_Limited_2026-05-15.pdf"): [
        "https://nsearchives.nseindia.com/corporate/ZUARI_15052026141051_zaclboardmeetingoutcome15052026.pdf",
        "https://nsearchives.nseindia.com/corporate/ZUARI_15052026140726_zaclboardmeetingoutcome15052026.pdf",
        "https://nsearchives.nseindia.com/corporate/ZUARI_15052026140446_zaclboardmeetingoutcome15052026.pdf",
    ],
    Path("downloads/NSE/MPS_Limited_2026-05-15.pdf"): [
        "https://nsearchives.nseindia.com/corporate/MPSLIMITED_15052026140804_OutcomeofBM.pdf",
    ],
    Path("downloads/NSE/Solar_Industries_India_Limited_2026-05-15.pdf"): [
        "https://nsearchives.nseindia.com/corporate/SOLARINDS_15052026142604_Merged_Result_final_Signed.pdf",
    ],
    Path("downloads/NSE/Hindustan_Copper_Limited_2026-05-15.pdf"): [
        "https://nsearchives.nseindia.com/corporate/HINDCOPPERMKD_15052026161745_OutcomeofBoardMeeting15052026.pdf",
        "https://nsearchives.nseindia.com/corporate/HINDCOPPERMKD_15052026161250_OutcomeofBoardMeeting15052026.pdf",
    ],
    Path("downloads/NSE/Arihant_Superstructures_Limited_2026-05-15.pdf"): [
        "https://nsearchives.nseindia.com/corporate/ARIHANTSUP_15052026170044_ASL_Outcome_of_BM_15052026_new.pdf",
        "https://nsearchives.nseindia.com/corporate/ARIHANTSUP_15052026163846_ASL_Outcome_of_BM_15052026.pdf",
    ],
}


async def main() -> None:
    """Download candidate PDFs, validate them, and replace broken originals."""

    repair_dir = Path("downloads") / "repair_candidates"
    backup_dir = Path("downloads") / "corrupt_backup"
    repair_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(headers=NSE_HEADERS, follow_redirects=True, timeout=90) as client:
        await request_with_retries(client, "GET", NSE_BASE_URL, headers=NSE_HEADERS)
        for target, urls in REPAIR_TARGETS.items():
            best_candidate = None
            best_size = 0
            for idx, url in enumerate(urls, start=1):
                candidate = repair_dir / f"{target.stem}_{idx}.pdf"
                try:
                    response = await request_with_retries(client, "GET", url, headers=NSE_HEADERS)
                except Exception as exc:
                    print(f"FAILED_DOWNLOAD|{target}|{idx}|{exc}")
                    continue
                candidate.write_bytes(response.content)
                valid = _valid_pdf(candidate)
                print(f"CANDIDATE|{target}|{idx}|size={candidate.stat().st_size}|valid={valid}|{url}")
                if valid and candidate.stat().st_size > best_size:
                    best_candidate = candidate
                    best_size = candidate.stat().st_size
            if not best_candidate:
                print(f"NOT_REPAIRED|{target}")
                continue
            backup = backup_dir / target.name
            if target.exists():
                target.replace(backup)
            best_candidate.replace(target)
            print(f"REPAIRED|{target}|size={target.stat().st_size}|backup={backup}")


def _valid_pdf(path: Path) -> bool:
    """Return whether a downloaded file can be opened as a non-empty PDF."""

    try:
        if not path.read_bytes().startswith(b"%PDF"):
            return False
        import fitz

        document = fitz.open(path)
        page_count = document.page_count
        document.close()
        return page_count > 0
    except Exception:
        return False


if __name__ == "__main__":
    asyncio.run(main())
