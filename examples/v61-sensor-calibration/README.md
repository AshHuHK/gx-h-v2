# Period-specific sensor calibration

This Goal compares paired voltage readings with reference values. The educational dataset contains four observations per period, with the same four reference voltages in each. All voltages are in mV. The CSV, method implementation and grant pin exact input and method bytes. The example grant permits one CPU Job and two model turns, with 300,000 tokens available for admission.

From the release root, install the package using the reviewed digest in `package-pin.json`.

```sh
pin=$(python3 -c "import json; print(json.load(open('examples/v61-sensor-calibration/package-pin.json'))['sha256'])")
bin/gx-p0 plugin install examples/plugins/sensor_calibration --sha256 "$pin"
bin/gx-p0 plugin verify org.example.gx.sensor-calibration@1.0.0
state="$PWD/var/calibration-study"
bin/gx-p0 start --state "$state" --run-key calibration \
  --goal "$(cat examples/v61-sensor-calibration/goal.md)" \
  --goal-config examples/v61-sensor-calibration/grant.json
bin/gx-p0 status --state "$state"
```

To ask your configured Lead to run the study, use `bin/gx-p0 run --state "$state" --timeout 180`. That command can consume provider tokens. To reproduce the isolated scripted trial with a real CPU Job, run the following from the release root with the deployed host and Clash paths. Use fresh locations for the two outputs.

```sh
python3 -B acceptance/phase2a/regression/loopback.py \
  --clash "$GX_CLASH" --receipt /tmp/gx61-csv-network.json -- \
  python3 -B acceptance/v61/csv_loopback.py --binary "$GX_CODEX" --out /tmp/gx61-csv-evidence
```

The method computes one signed error per row as reading minus paired reference. Each period receives a mean signed error, a mean absolute error and a root-mean-square error. The raw artifact includes sign counts. The next Lead receives the period summaries and their source artifact reference, which it can use to read the full record. An independent decimal calculation checks all six reported values to within 1e-12 mV. Differences describe these sample periods; identifying the source of a change would require additional measurements.
