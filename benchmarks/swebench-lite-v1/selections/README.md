# Fixed SWE-bench Lite selections

`fetch-dataset` and `import-dataset` write `capability-30.json` and
`context-stress-10.json` here. Review and commit those two ordered-ID manifests
before a formal generation run. A clean formal run refuses any snapshot whose
dataset fingerprint, ordered IDs, or selection fingerprint differs from the
committed manifest.
