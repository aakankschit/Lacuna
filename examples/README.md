# Worked example

A tiny repository and three notes, so `lacuna lit synth` and `lacuna lit verify` can be run end to end with
no PDFs, no parser installed and no document corpus.

```bash
LAC=<plugin dir>/bin/lacuna

$LAC lit synth --notes examples/notes --repo examples/demo-repo --out /tmp/plan.md \
    --kind plan --objective "What should we build next?"
$LAC lit verify --target /tmp/plan.md --repo examples/demo-repo --notes examples/notes
```

Expect the synthesiser to propose work against `demo-repo`'s real files, and the verifier to report
either nothing or a specific unverifiable claim. Both stages make real API calls, so they cost real
money — a few cents at the default models.

## The notes are fictional

**The three notes describe papers that do not exist.** They were written by hand to exercise the
note format, not to represent literature. Do not cite them, and do not treat their numbers as real
findings. In a real run these files are written by the reader agent from documents you supply.
