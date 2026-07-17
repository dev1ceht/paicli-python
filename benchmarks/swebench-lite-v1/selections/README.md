# Fixed SWE-bench Lite selections

`fetch-dataset` and `import-dataset` write `capability-30.json` and
`context-stress-10.json` here. The claim-eligible `context-stress-5-v1.json`
is a versioned five-task prefix of that frozen source population, while
`flask-pilot-1-v1.json` is development-only. Review and commit ordered-ID manifests
before a formal generation run. A clean formal run refuses any snapshot whose
dataset fingerprint, ordered IDs, or selection fingerprint differs from the
committed manifest.
