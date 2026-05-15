fn main() {
    if let Err(err) = nexus_bench::cli::run() {
        eprintln!("{err}");
        std::process::exit(1);
    }
}
