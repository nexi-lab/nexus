export interface CommandPaletteItem {
  readonly id: string;
  readonly title: string;
  readonly section: string;
  readonly hint?: string;
  readonly keywords?: readonly string[];
  readonly run: () => void;
}

export function filterCommandPaletteItems(
  items: readonly CommandPaletteItem[],
  query: string,
): readonly CommandPaletteItem[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return items;

  return items
    .map((item) => ({
      item,
      haystack: [
        item.title,
        item.section,
        item.hint ?? "",
        ...(item.keywords ?? []),
      ].join(" ").toLowerCase(),
    }))
    .filter(({ haystack }) => haystack.includes(needle))
    .sort((a, b) => {
      const aStarts = a.item.title.toLowerCase().startsWith(needle) ? 0 : 1;
      const bStarts = b.item.title.toLowerCase().startsWith(needle) ? 0 : 1;
      if (aStarts !== bStarts) return aStarts - bStarts;
      return a.item.title.localeCompare(b.item.title);
    })
    .map(({ item }) => item);
}
