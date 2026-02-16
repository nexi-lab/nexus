use std::collections::HashMap;

fn search_files(pattern: &str, files: &HashMap<String, String>) -> Vec<String> {
    let mut results = Vec::new();
    for (path, content) in files {
        if content.contains(pattern) {
            results.push(path.clone());
        }
    }
    results
}

fn main() {
    let mut files = HashMap::new();
    files.insert("test.txt".to_string(), "hello world".to_string());
    files.insert("data.txt".to_string(), "foo bar baz".to_string());

    let matches = search_files("hello", &files);
    println!("Found {} matches", matches.len());
}
