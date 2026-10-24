import os
import sys

# Ensure hrms-bot root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.ingest import process_and_ingest_document
from app.vectorstore import init_vectorstore, search_policies


def ingest_sample_policies():
    """Ingests all policy documents in the sample_docs directory into Qdrant `hr_policies`."""
    print("Initializing vectorstore collections...")
    init_vectorstore()

    sample_docs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sample_docs"))
    if not os.path.exists(sample_docs_dir):
        print(f"Error: Directory {sample_docs_dir} not found.")
        return

    doc_files = [f for f in os.listdir(sample_docs_dir) if f.endswith((".md", ".txt", ".pdf"))]
    print(f"Found {len(doc_files)} policy documents in {sample_docs_dir}:\n")

    total_chunks = 0
    for filename in sorted(doc_files):
        file_path = os.path.join(sample_docs_dir, filename)
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        
        chunks = process_and_ingest_document(
            file_bytes=file_bytes,
            filename=filename,
            source=filename,
        )
        total_chunks += chunks
        print(f"  [OK] {filename:<38} -> {chunks:>2} chunks ingested")

    print(f"\nTotal policy chunks ingested: {total_chunks}")

    # Run quick verification search
    test_query = "how many casual leaves do I get"
    print(f"\nRunning verification query: '{test_query}'...")
    results = search_policies(test_query, top_k=3)
    for i, res in enumerate(results, 1):
        print(f"\nResult #{i} (Score: {res['score']:.4f}) [Source: {res['source']} | Chunk {res['chunk_index']}]:")
        print(f"  {res['text'][:150]}...")


if __name__ == "__main__":
    ingest_sample_policies()
