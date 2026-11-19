# Final regression and integration report

| Suite | Verified baseline | Total | Passed | Failed | Errors | Skipped | Execution |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 with coverage | 157 | 172 | 172 | 0 | 0 | 0 | 14.23 s |
| Bundled S2 | 28 | 28 | 28 | 0 | 0 | 0 | 1.74 s |
| Total | 185 | 200 | 200 | 0 | 0 | 0 | 15.97 s |

The increase is 13 parking/lifecycle checks and two video-renderer checks. Existing tests were preserved. The S1 suite includes 81 integration tests and 45 dedicated fault-injection tests. A separate combined targeted run passed 60 tests in 11.93 s. S1 line coverage is **61%**, compared with 59% at the verified baseline.

Linux x86_64, Python 3.12, CPU PyTorch, OpenCV and FFmpeg. Test cases used local fixtures, SQLite and the loopback service. No production publishing occurred. The API harness passed 44 HTTP checks, Swagger/ReDoc and schema assertions passed 10 checks, and both regenerated probe videos decoded completely. Real site deployments remain outside scope.

- [S1 JUnit](raw/s1.xml), [S2 JUnit](raw/s2.xml), [targeted JUnit](raw/unified-targeted.xml)
- [S1 console log](raw/s1.log), [S2 console log](raw/s2.log), [fault and handover console log](raw/unified-targeted.log)
- [S1 coverage](raw/coverage.txt) and [coverage including the evidence renderer](raw/coverage-including-evidence-renderer.txt)
- [Reproduction commands](FINAL_REPRODUCTION_COMMANDS.md)

## Submission rerun of the exact preserved release

Fresh extraction produced 172 S1 passes in 14.12 seconds and 28 S2 passes in 1.40 seconds. No failures, errors or skips. S1 line coverage remained 61%. See [S1 output](raw/submission-rerun/s1.log) and [S2 output](raw/submission-rerun/s2.log). API/browser and real-footage observations elsewhere in this report are preserved execution evidence.
