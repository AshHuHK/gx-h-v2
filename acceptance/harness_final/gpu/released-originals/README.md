# B 的原始材料

本次 Run 首个预测坐标已发布后，保管席向研究席同时登记 1F6S mmCIF 和显式位置对照。另存的 legacy PDB 供独立 QCP 复算使用。两种坐标文件描述同一次实验沉积。UniProt 原件记录了本题供试的天然成熟链、范围与序列版本。[UniProt P00711 原始记录](https://rest.uniprot.org/uniprotkb/P00711.json) · [wwPDB 坐标](https://files.rcsb.org/download/1F6S.cif) · [wwPDB legacy PDB](https://files.rcsb.org/download/1F6S.pdb)

重新解压三份原件至指定的局部复核目录，再复制两份取得描述和位置对照，即可复算。文件内容摘要按解压后的字节核对。`map-origin-before-preflight.json` 保存首次投放演练失败时的取得描述。[事前注册](../preregistration.json) · [首个错误和修正摘要](../preflight-addendum.json)

```bash
mkdir -p "$VERIFY_MATERIALS"
for name in 1F6S.cif 1F6S.pdb P00711.json; do
  gzip -dc "acceptance/harness_final/gpu/released-originals/$name.gz" > "$VERIFY_MATERIALS/$name"
done
cp acceptance/harness_final/gpu/released-originals/{mapping,acquisition,map-origin}.json "$VERIFY_MATERIALS/"
```
