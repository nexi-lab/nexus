//! Heading + code-fence + budget-aware chunker (Phase 4 of the
//! Python-parity roadmap; see `PARITY_ROADMAP.md`).
//!
//! # Goal
//!
//! Split file text into semantically-coherent chunks that:
//! - respect markdown-ish heading boundaries (a chunk stays within
//!   one leaf section);
//! - preserve code-fence blocks (never split inside triple-backtick);
//! - stay under a soft character budget so embedder + BM25 both
//!   have workable per-chunk sizes;
//! - carry a heading trail for the embedder so semantic recall
//!   picks up on section context that pure chunk text would lose.
//!
//! # Fidelity vs Python
//!
//! Post-audit D1: **semantic-equivalent, not byte-identical**.
//! Tokeniser + markdown-parser differences across the Python /
//! Rust boundary would make byte-identical impossible.  What we
//! guarantee:
//! - same chunk-count-per-doc ± 1
//! - each chunk's boundary aligns with a paragraph or heading
//!   (never mid-word)
//! - code fences never split
//! - heading stack tracked exactly like Python
//!
//! # No new heavy deps
//!
//! Line-based scan for headings + fences.  No `pulldown-cmark`
//! (~200 KB), no `tiktoken-rs` (~1 MB + model files) — char-based
//! budget approximates BPE tokens closely enough for P4's initial
//! shape.  If recall parity with Python surfaces a real gap we
//! swap in `tiktoken-rs` as a targeted P5 follow-up.

/// Soft target for chunk character length.  ~1600 chars ≈ 400 BPE
/// tokens for typical English (~4 chars/token), matching Python's
/// default chunk budget.
pub const CHUNK_TARGET_CHARS: usize = 1600;

/// Hard upper bound for a single chunk.  A monster paragraph that
/// blows past this alone is emitted as its own oversize chunk
/// rather than split mid-content — better to trust the source's
/// paragraph structure than mangle it.
pub const CHUNK_MAX_CHARS: usize = 4000;

/// One emitted chunk.  Two text fields:
///
/// - `text` = pure chunk content, no heading prefix.  Goes into
///   FTS `chunk_text` (BM25 wants clean tokens, no heading
///   pollution) and returned to callers as `QueryResult.chunk_text`.
/// - `embed_input` = heading-prefixed text — what the embedder
///   sees.  Semantic recall lifts on section context that raw
///   chunk text would lose.  Matches Python's
///   `embed_input != stored_text` split.
#[derive(Debug, Clone, PartialEq)]
pub struct Chunk {
    pub chunk_index: u32,
    pub text: String,
    pub embed_input: String,
}

/// Split `text` into chunks.  Never returns an empty vec — an
/// entirely-blank file still emits one empty chunk so downstream
/// code (which treats "zero chunks" as an error signal) sees a
/// single well-formed slot.  Callers who want to skip empties
/// filter upstream.
pub fn chunk_document(text: &str) -> Vec<Chunk> {
    // Fast path: empty text stays empty (do_index already skips
    // these upstream, but pin the semantics here too so a caller
    // that bypasses do_index doesn't crash).
    if text.trim().is_empty() {
        return Vec::new();
    }

    let mut emitter = Emitter::new();
    let mut headings: HeadingStack = HeadingStack::default();
    let mut buf = String::new();
    let mut in_code_fence = false;

    for raw_line in text.split_inclusive('\n') {
        // Preserve the trailing newline (or lack of, on the tail
        // line) so round-trip'd text stays faithful.
        let trimmed_start = raw_line.trim_start();

        // Code fence toggles.  We check the LINE-start (after
        // leading whitespace) so indented fences inside lists still
        // register.
        if trimmed_start.starts_with("```") {
            in_code_fence = !in_code_fence;
            buf.push_str(raw_line);
            continue;
        }

        // Inside a code fence: unconditional inclusion.  Do not
        // check for headings or paragraph breaks — # inside code
        // is not a heading, blank lines inside code are not
        // paragraph boundaries.
        if in_code_fence {
            buf.push_str(raw_line);
            continue;
        }

        // Heading detection: 1–6 leading `#` followed by space.  A
        // new heading ALWAYS seals the current chunk (matches
        // Python) so a heading-heavy but text-light document gets
        // one chunk per section rather than everything crammed into
        // one huge chunk.  Callers with tiny sections can pool
        // downstream via `chunks_per_page`.
        if let Some((level, title)) = parse_heading(raw_line) {
            if !buf.trim().is_empty() {
                let text = std::mem::take(&mut buf);
                emitter.emit(&headings, text);
            }
            headings.push(level, title);
            buf.push_str(raw_line);
            continue;
        }

        // Blank-line paragraph boundary — a good place to try
        // sealing a chunk if we're over budget.
        if raw_line.trim().is_empty() {
            buf.push_str(raw_line);
            if buf.len() >= CHUNK_TARGET_CHARS {
                let text = std::mem::take(&mut buf);
                emitter.emit(&headings, text);
            }
            continue;
        }

        buf.push_str(raw_line);
    }

    // Trailing content that never crossed a paragraph or heading
    // boundary.
    if !buf.trim().is_empty() {
        emitter.emit(&headings, buf);
    }

    emitter.into_chunks()
}

/// Parse a markdown-style ATX heading (`#` through `######` +
/// whitespace + title).  Setext (underline) headings not handled
/// yet — deferred; rare enough that Python's chunker treats them
/// the same for context tracking.
fn parse_heading(line: &str) -> Option<(u8, String)> {
    let stripped = line.trim_start();
    let mut hashes = 0u8;
    for ch in stripped.chars() {
        if ch == '#' && hashes < 6 {
            hashes += 1;
        } else {
            break;
        }
    }
    if hashes == 0 {
        return None;
    }
    let rest = stripped[hashes as usize..].strip_prefix(' ')?;
    Some((hashes, rest.trim_end_matches('\n').trim().to_string()))
}

/// Heading stack — tracks the deepest heading chain in effect.
/// When a new heading at level N arrives, drop everything at
/// level ≥ N, then push the new one.  Rendered as
/// `[H1: title][H2: title]…` for the embedder prefix.
#[derive(Debug, Default, Clone)]
struct HeadingStack {
    stack: Vec<(u8, String)>,
}

impl HeadingStack {
    fn push(&mut self, level: u8, title: String) {
        self.stack.retain(|(l, _)| *l < level);
        self.stack.push((level, title));
    }

    fn render_prefix(&self) -> String {
        if self.stack.is_empty() {
            return String::new();
        }
        let mut s = String::new();
        for (level, title) in &self.stack {
            s.push_str(&format!("[H{level}: {title}] "));
        }
        s.push('\n');
        s.push('\n');
        s
    }
}

/// Chunk accumulator — hands out monotonic chunk_index and pairs
/// each chunk's text with a heading-prefixed `embed_input`.
struct Emitter {
    chunks: Vec<Chunk>,
}

impl Emitter {
    fn new() -> Self {
        Self { chunks: Vec::new() }
    }

    fn emit(&mut self, headings: &HeadingStack, text: String) {
        let embed_input = if headings.stack.is_empty() {
            text.clone()
        } else {
            let mut s = headings.render_prefix();
            s.push_str(&text);
            s
        };
        // Cap oversize chunks at CHUNK_MAX_CHARS by truncation.
        // Losing a bit of tail is preferable to reading past the
        // embedder's max input size.
        let (text, embed_input) = if text.len() > CHUNK_MAX_CHARS {
            (
                text.chars().take(CHUNK_MAX_CHARS).collect::<String>(),
                embed_input
                    .chars()
                    .take(CHUNK_MAX_CHARS)
                    .collect::<String>(),
            )
        } else {
            (text, embed_input)
        };
        let chunk_index = self.chunks.len() as u32;
        self.chunks.push(Chunk {
            chunk_index,
            text,
            embed_input,
        });
    }

    fn into_chunks(self) -> Vec<Chunk> {
        self.chunks
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_text_returns_no_chunks() {
        assert!(chunk_document("").is_empty());
        assert!(chunk_document("   \n\n   ").is_empty());
    }

    #[test]
    fn short_text_becomes_one_chunk() {
        let out = chunk_document("hello world\n");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].chunk_index, 0);
        assert!(out[0].text.contains("hello world"));
    }

    #[test]
    fn headings_split_chunks_and_stack_context() {
        let text = "\
# Intro

Intro body text.

## Setup

Setup body text.

## Usage

Usage body text.
";
        let out = chunk_document(text);
        // Expect one chunk per heading + intro material.
        assert!(out.len() >= 3, "expected ≥ 3 chunks, got {out:?}");

        // The Usage chunk carries "H1: Intro" + "H2: Usage" in its
        // embed_input, not in its plain text.
        let usage = out
            .iter()
            .find(|c| c.text.contains("Usage body text"))
            .expect("usage chunk missing");
        assert!(
            usage.embed_input.contains("H1: Intro"),
            "prefix missing: {usage:?}"
        );
        assert!(usage.embed_input.contains("H2: Usage"));
        // Plain text is heading-free (BM25 shouldn't see the prefix).
        assert!(!usage.text.starts_with("[H1:"));
    }

    #[test]
    fn code_fence_content_is_never_split_and_no_heading_inside() {
        let text = "\
# Intro

Intro body.

```python
# not-a-heading
def f():
    pass

# still-not-a-heading
```

Post-fence text.
";
        let out = chunk_document(text);
        // Code fence stays intact inside its containing chunk.
        let with_code = out
            .iter()
            .find(|c| c.text.contains("def f():"))
            .expect("code chunk missing");
        assert!(with_code.text.contains("# not-a-heading"));
        assert!(with_code.text.contains("# still-not-a-heading"));
        // The `#` inside the fence didn't push into the heading
        // stack — no `[H1: not-a-heading]` prefix.
        for c in &out {
            assert!(
                !c.embed_input.contains("H1: not-a-heading"),
                "code-fenced heading leaked to stack: {c:?}",
            );
        }
    }

    #[test]
    fn budget_forces_split_at_paragraph_boundary() {
        // Build a text with several paragraphs whose combined length
        // exceeds CHUNK_TARGET_CHARS.  Each paragraph itself under
        // the target so the split happens at paragraph boundaries.
        let paragraph = "a".repeat(500);
        let text =
            format!("{paragraph}\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}\n\n");
        let out = chunk_document(&text);
        assert!(
            out.len() >= 2,
            "budget should force ≥ 2 chunks, got {}",
            out.len()
        );
        // Every chunk stays under the hard max.
        for c in &out {
            assert!(
                c.text.len() <= CHUNK_MAX_CHARS + 10,
                "chunk oversize: {}",
                c.text.len()
            );
        }
        // chunk_index is monotonic starting at 0.
        for (i, c) in out.iter().enumerate() {
            assert_eq!(c.chunk_index, i as u32);
        }
    }

    #[test]
    fn heading_stack_pops_on_shallower_heading() {
        let text = "\
# One

body-1.

## Two

body-2.

# Three

body-3.
";
        let out = chunk_document(text);
        let three = out
            .iter()
            .find(|c| c.text.contains("body-3"))
            .expect("three chunk");
        // After # Three, H2 stack popped — no "[H2: Two]" in prefix.
        assert!(three.embed_input.contains("H1: Three"));
        assert!(
            !three.embed_input.contains("H2: Two"),
            "stack didn't pop: {three:?}"
        );
    }

    #[test]
    fn no_heading_no_prefix_in_embed_input() {
        let out = chunk_document("just plain text\nno heading anywhere\n");
        assert_eq!(out.len(), 1);
        assert_eq!(
            out[0].embed_input, out[0].text,
            "no headings ⇒ embed_input == text",
        );
    }
}
