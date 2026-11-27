# Public local research module example

The Goal is to determine which coordinate comparisons the two archived depositions support and identify the next measurement that would distinguish plausible explanations. This is public development material for a scripted engineering replay. The rubric and service for any future unseen-case evaluation belong to its independent registration.

| Material | What the researcher can learn |
| --- | --- |
| `models.json` | The coordinate model, revision, refinement resolution, provenance and shared primary publication for each entry. |
| `workflows.json` | The inputs, scientific scope, cost and output of two registered Goal methods and a historical static workflow. |
| `evaluations.json` | The numerical reviewer, the shared data behind the check, its endpoint and the outstanding coverage gap. |
| `deposition-excerpt.txt`, `method-notes.md`, `deposition-note.xml` | Attributable public reading fixtures in three formats. The JATS note distinguishes the title of the cited article from the 2O0O deposition's own resolution. |
| `examples/tl1a_geometry_v3/README.md` | An existing published GX methods document, registered as a literature source for the narrow claims its own paragraphs support. |
| The 2RE9 and 2O0O mmCIF files | Original, version-pinned coordinates for a separately commissioned inventory or follow-up measurement. |

The model names in the public cards refer to crystallographic models. The two entries share a publication. An independent numerical fit of the same atoms can establish agreement between implementations. Claims about solution behavior or sampling independence require additional evidence. [Primary article citation](https://doi.org/10.1016/j.bbrc.2007.09.097) · [Depositions and source versions](../../../acceptance/v61/adaptive/data/provenance.json)

`grant.json` includes A's data inventory and the three roles in the research-survey package. GX registers the listed materials and captures their checksums. The Lead delegates individual references from its frozen brief. The survey reads public method metadata and assigned public cards. The offline reader obtains original passages and read receipts through GX. A later hypothesis module can receive a published reading or inventory report and propose an observation that would distinguish alternatives. The local unit tests exercise registration of a further, independently installed module. [Shipped declarations](../../../gx-h/plugins/installed/org.gx.research-survey/1.0.0/plugin.py) · [Test plugin](../v62_reading_check/plugin.py)

From the product root, this command prepares a Goal and its initial frozen brief. Use a fresh state directory for each independent study.

```sh
GX_CODEX_OFFLINE=1 bin/gx-p0 start --state /tmp/gx-v62-public-materials --run-key public-module-example \
  --goal 'Which coordinate comparisons do 2RE9 and 2O0O support, and which observation would distinguish competing explanations?' \
  --goal-config examples/plugins/v62_public_materials/grant.json
```
