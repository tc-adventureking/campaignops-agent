import argparse
from pathlib import Path

from app.observability.store import RunStore
from app.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = Settings()
    events = RunStore(settings.state_path).export_trace(args.trace_id)
    content = "\n".join(event.model_dump_json() for event in events) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
