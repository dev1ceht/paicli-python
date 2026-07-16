# Use zero temperature for baseline benchmark sampling

PaiCLI's baseline live coding benchmark will explicitly request model temperature zero rather than inheriting the production default, reducing sampling variance and making repeated measurements easier to interpret. This is a benchmark configuration rather than a claim of determinism—providers may still vary—and runs using another temperature receive a different configuration identity instead of being aggregated with the baseline.
