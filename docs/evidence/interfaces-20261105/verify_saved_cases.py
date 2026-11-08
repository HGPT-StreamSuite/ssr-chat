"""Verify new examples against saved real c9 native receipts, without re-execution."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT.parents[1] / "evidence/development/native-review-web-tests/6349288e530b4853b899724a82955173"
CASES = ROOT / "fixtures/contracts/review-native.v1.cases.json"
SNAPSHOT = "c9d9637d2ad9f2577c8f77320874b2d1d98702554fc0e0049831fdeff67a91be"


def main():
    spec = json.loads(CASES.read_text(encoding="utf-8"))
    rows = []
    for case in spec["valid"]:
        name = "purpose-denied" if case["id"] == "denied" else case["id"]
        path = SOURCE / (name + ".response.json")
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        response = wrapper["body"]
        expected = case["expected"]
        assert wrapper["handler_status"] == 200
        assert response["pipeline_exit_code"] == expected["pipeline_exit_code"]
        assert response["result"]["status"] == expected["status"]
        assert response["source_unchanged"] is True
        assert response["source_snapshot_sha256"] == SNAPSHOT
        assert response["evidence"]["input"]["text"] == case["request"]["text"]
        assert response["tool"] == case["request"]["tool"]
        for field, value in spec["invariants"].items():
            assert response[field] == value
        if "reason_code" in expected:
            assert response["result"]["reason_code"] == expected["reason_code"]
        if "record_count" in expected:
            assert len(response["evidence"]["records"]) == expected["record_count"]
        if "prompt" in expected:
            assert (response["evidence"]["prompt"] is not None) == expected["prompt"]
        if "reader_count" in expected:
            assert len(response["evidence"]["native_reader_receipts"]) == expected["reader_count"]
        rows.append({"case": case["id"], "status": "matched_saved_real_receipt", "receipt": str(path),
                     "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "snapshot": SNAPSHOT})
    output = Path(__file__).with_name("saved-cases-verification.json")
    with output.open("x", encoding="utf-8") as stream:
        json.dump({"method": "read_existing_native_receipts_no_rerun", "case_file_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
                   "scope": "historical c9 graph; excludes new interface_version metadata and later code/dependencies", "results": rows}, stream, indent=2)
    print("Matched 6 machine-readable examples to saved real native receipts; no native rerun.")


if __name__ == "__main__":
    main()
