# Active background-task cancellation

PaiCLI will let a Runtime background-task cancellation signal wake the owning asyncio event loop and cancel its Agent task, so cancelable LLM and tool awaits release a worker promptly. Background shell commands run in their own process group and are terminated as a process tree when cancellation propagates; synchronous or externally irreversible operations remain outside this guarantee, and interactive Agent sessions do not use this Runtime cancellation path.
