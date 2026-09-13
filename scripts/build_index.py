from pathlib import Path

from app.settings import Settings
from app.tools.retrieval import LocalRetriever


def main() -> None:
    retriever = LocalRetriever(Settings().knowledge_dir)
    retriever.write_index(Path("data/indexes/knowledge.json"))
    print(f"Indexed {len(retriever.chunks)} chunks; version={retriever.version}")


if __name__ == "__main__":
    main()
