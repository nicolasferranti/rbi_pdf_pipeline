# rbi_pdf_pipeline


Split PDFs:
```
find . -type f -iname '*.pdf' -print0 |
while IFS= read -r -d '' pdf; do
    directory=$(dirname "$pdf")
    filename=$(basename "$pdf")
    name="${filename%.*}"
    output_dir="$directory/$name"

    mkdir -p "$output_dir"
    pdfseparate "$pdf" "$output_dir/${name}_page_%04d.pdf"
done
```
