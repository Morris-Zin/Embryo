use crate::models::CatalogItem;
use memchr::memmem;
use std::collections::HashMap;

#[derive(Clone, Debug)]
struct IndexedItem {
    row: CatalogItem,
    folded_code: String,
    folded_name: String,
    folded_group: String,
    folded_brand: String,
    folded_barcode_text: String,
    search_text: String,
}

impl IndexedItem {
    fn new(row: CatalogItem) -> Self {
        let folded_code = fold(&row.item_code);
        let folded_name = fold(row.item_name.as_deref().unwrap_or(""));
        let folded_group = fold(row.item_group.as_deref().unwrap_or(""));
        let folded_brand = fold(row.brand.as_deref().unwrap_or(""));
        let folded_barcode_text = fold(row.barcode_text.as_deref().unwrap_or(""));
        let search_text = [
            folded_code.as_str(),
            folded_name.as_str(),
            folded_group.as_str(),
            folded_brand.as_str(),
            folded_barcode_text.as_str(),
        ]
        .join("\u{1f}");
        Self {
            row,
            folded_code,
            folded_name,
            folded_group,
            folded_brand,
            folded_barcode_text,
            search_text,
        }
    }
}

fn fold(value: &str) -> String {
    value.to_lowercase()
}

#[derive(Default)]
pub struct CatalogIndex {
    items: Vec<IndexedItem>,
    exact_barcode: HashMap<String, usize>,
}

impl CatalogIndex {
    pub fn rebuild(mut rows: Vec<CatalogItem>) -> Self {
        rows.retain(|row| !row.deleted && !row.item_code.is_empty());
        rows.sort_by(|left, right| left.item_code.cmp(&right.item_code));
        rows.dedup_by(|left, right| {
            if left.item_code == right.item_code {
                *left = right.clone();
                true
            } else {
                false
            }
        });

        let mut exact_barcode = HashMap::new();
        let items: Vec<IndexedItem> = rows.into_iter().map(IndexedItem::new).collect();
        for (position, item) in items.iter().enumerate() {
            if let Some(barcode) = item.row.barcode.as_deref() {
                let folded = fold(barcode);
                if !folded.is_empty() {
                    exact_barcode.entry(folded).or_insert(position);
                }
            }
            for barcode in item.folded_barcode_text.split_whitespace() {
                exact_barcode.entry(barcode.to_string()).or_insert(position);
            }
        }
        Self {
            items,
            exact_barcode,
        }
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn search(&self, term: &str, limit: usize) -> Vec<CatalogItem> {
        let folded_term = fold(term.trim());
        if folded_term.is_empty() || limit == 0 {
            return Vec::new();
        }
        let limit = limit.min(25);
        let mut exact = Vec::new();
        let mut prefix = Vec::new();
        let mut substring = Vec::new();

        if let Some(position) = self.exact_barcode.get(&folded_term) {
            // A complete barcode identifies one sellable unit. Returning immediately avoids
            // scanning the whole catalog on the dominant cashier path.
            return vec![self.items[*position].row.clone()];
        }

        for item in &self.items {
            if exact
                .iter()
                .any(|row: &CatalogItem| row.item_code == item.row.item_code)
            {
                continue;
            }
            let is_prefix = item.folded_code.starts_with(&folded_term)
                || item.folded_name.starts_with(&folded_term)
                || item.folded_group.starts_with(&folded_term)
                || item.folded_brand.starts_with(&folded_term);
            if is_prefix {
                prefix.push(item.row.clone());
            } else if memmem::find(item.search_text.as_bytes(), folded_term.as_bytes()).is_some() {
                substring.push(item.row.clone());
            }
        }

        exact.extend(prefix);
        exact.extend(substring);
        exact.truncate(limit);
        exact
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(code: &str, name: &str, barcode_text: &str) -> CatalogItem {
        CatalogItem {
            item_code: code.to_string(),
            item_name: Some(name.to_string()),
            stock_uom: Some("Nos".to_string()),
            item_group: Some("Tools".to_string()),
            brand: Some("House".to_string()),
            barcode: barcode_text.split_whitespace().next().map(str::to_string),
            barcode_text: Some(barcode_text.to_string()),
            rate: 1.0,
            actual_qty: 2.0,
            modified: None,
            deleted: false,
        }
    }

    #[test]
    fn exact_barcode_wins_then_prefix_then_substring() {
        let index = CatalogIndex::rebuild(vec![
            row("A-PREFIX", "Red Hammer", "111"),
            row("B-SUBSTRING", "Large Red Nail", "222"),
            row("C-EXACT", "Other", "red"),
        ]);
        let hits = index.search("red", 10);
        let codes: Vec<&str> = hits.iter().map(|row| row.item_code.as_str()).collect();
        assert_eq!(codes, vec!["C-EXACT"]);

        let hits = index.search("ham", 10);
        let codes: Vec<&str> = hits.iter().map(|row| row.item_code.as_str()).collect();
        assert_eq!(codes, vec!["A-PREFIX"]);
    }

    #[test]
    fn substring_matches_do_not_hide_later_prefix_matches() {
        let mut rows = Vec::new();
        for index in 0..50 {
            rows.push(row(
                &format!("S-{index:02}"),
                &format!("Large red substring {index}"),
                &format!("9{index:03}"),
            ));
        }
        rows.push(row("RED-PREFIX", "Red final", "8888"));
        let index = CatalogIndex::rebuild(rows);
        let hits = index.search("red", 10);
        assert_eq!(
            hits.first().map(|row| row.item_code.as_str()),
            Some("RED-PREFIX")
        );
    }

    #[test]
    fn deleted_rows_are_removed() {
        let mut deleted = row("A", "A", "1");
        deleted.deleted = true;
        let index = CatalogIndex::rebuild(vec![deleted]);
        assert_eq!(index.len(), 0);
    }

    #[test]
    fn burmese_names_are_searchable_without_lossy_ascii_folding() {
        let index = CatalogIndex::rebuild(vec![row("MM-1", "ဆန်၀ါး ၅ ကီလို", "111")]);
        let hits = index.search("ဆန်", 10);
        assert_eq!(hits.first().map(|row| row.item_code.as_str()), Some("MM-1"));
    }

    proptest::proptest! {
        #[test]
        fn exact_ascii_barcode_is_a_single_fast_result(barcode in "[0-9]{8,14}") {
            let mut exact = row("EXACT", "Barcode target", &barcode);
            exact.barcode = Some(barcode.clone());
            let index = CatalogIndex::rebuild(vec![
                row("PREFIX", &format!("{barcode} prefix"), "99999999999999"),
                exact,
            ]);
            let hits = index.search(&barcode, 25);
            proptest::prop_assert_eq!(hits.len(), 1);
            proptest::prop_assert_eq!(hits[0].item_code.as_str(), "EXACT");
        }
    }
}
