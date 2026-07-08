/// Whether Firebase initialized this run (google-services.json was wired at
/// build time). Set once in main(); read by the shell to decide whether to
/// start push registration. When false the app runs normally, just without
/// push — so debug/no-secret builds work.
bool firebaseReady = false;
