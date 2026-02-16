Trigram Index Test Corpus
========================

This directory contains sample files for testing the trigram index.
It includes Python, Rust, JavaScript, and plain text files.

The trigram index extracts all 3-byte sequences from file content
and builds an inverted index mapping trigrams to file IDs.

For example, the word "hello" contains these trigrams:
- hel
- ell
- llo

Search queries extract trigrams from the pattern and intersect
the posting lists to find candidate files.
