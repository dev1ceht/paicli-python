# Version and fingerprint benchmark suites

Published benchmark suites will be identified by both a versioned name and a content fingerprint covering their task prompts, fixtures, withheld acceptance material, and verifier definitions. Any change that can affect task behavior or correctness requires a new suite version, while prior results remain associated with their original identity; this costs additional version maintenance but prevents silently comparing runs produced by different tasks under the same label.
