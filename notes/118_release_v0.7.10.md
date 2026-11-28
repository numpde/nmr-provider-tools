# Provider SDK v0.7.10

Providers can derive batch adapters from declared scientific operations, share typed interpreter-tool decoding, and use the SDK service-lifetime runner. Scientific selection, model configuration and application policy remain provider-owned.

Analysis outcomes bind declared failure codes and encoded success payloads. Consumers upgrading from 0.7.9 must adopt the named outcome arguments and typed payload interface; existing positional result construction is not compatible.

UploadWorkspace retains private downloaded files until scientific readers and acquisition I/O retire. It refuses existing directories, closes admission during retirement, and retains files when cleanup is uncertain. JobUploads remains the acquisition, retry and received-byte budget owner. Collection snapshot reading checks Job/Upload identities and declared per-file and total sizes before verifying immutable source bytes.

Candidate consumer checks covered 137 SECS provider/snapshot tests, 31 MolDeTr binding/input/provider tests, real MolDeTr checkpoint execution through the SDK runner, and its workspace-to-scientific-input handoff. MolDeTr Job orchestration and delivery remain unfinished. These checks do not claim live deployment or released dependency-lock adoption.

The attached release qualification log records installed-artifact results. The established release configuration uses PRIVACY_TESTS=0; skipped privacy checks are not passing results.
