import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";

/**
 * Which report sections this reader is being shown.
 *
 * THE SERVER HAS ALREADY DECIDED. Every report arrives with the hidden
 * sections' data emptied by `api/redact.py`, so what this context controls is
 * only whether the empty container is drawn. That distinction matters when
 * reading this file: nothing here is a control, and a bug in it leaks a card
 * with no rows in it rather than a card with somebody's data in it.
 *
 * What it buys is honesty. A card that renders its own "no rows" empty state
 * reads as "this venue has none", which is a different and wrong statement.
 * Dropping the card entirely says nothing rather than something false.
 *
 * The list comes from the report payload itself (`report.visibility.hidden`)
 * rather than from a second fetch, so a report and the policy it was rendered
 * under can never be a version apart.
 */

interface VisibilityValue {
  hidden: ReadonlySet<string>;
  visible: (sectionId: string) => boolean;
  /** Any section on this tab still visible? Drives the tab bar. */
  visibleTab: (tab: string) => boolean;
  redacted: boolean;
}

// Defaults to showing everything. An empty policy is by far the common case,
// and a provider-less render — a component tested in isolation, or a tree that
// grows a new branch above the provider — should draw the whole report rather
// than nothing at all.
const EMPTY: VisibilityValue = {
  hidden: new Set<string>(),
  visible: () => true,
  visibleTab: () => true,
  redacted: false,
};

const VisibilityContext = createContext<VisibilityValue>(EMPTY);

export const useVisibility = () => useContext(VisibilityContext);

/** Shorthand for the common case, which is one card asking about itself. */
export const useVisible = (sectionId?: string) => {
  const { visible } = useContext(VisibilityContext);
  // No id means a card that predates the catalogue, or one deliberately not
  // hideable — the report header, a loading placeholder. Always shown.
  return sectionId ? visible(sectionId) : true;
};

export function VisibilityProvider(
  { hidden, children }: { hidden?: string[] | null; children: ReactNode },
) {
  const value = useMemo<VisibilityValue>(() => {
    const set = new Set(hidden || []);
    return {
      hidden: set,
      visible: (sectionId: string) => !set.has(sectionId),
      // Section ids are `<tab>.<thing>` by convention (see api/sections.py), so
      // a tab's membership is a prefix test and the frontend needs no copy of
      // the catalogue to answer this. That convention is asserted by
      // api/tests/test_sections.py — if it ever stops holding, this silently
      // starts answering "true" for every tab, so the test is what keeps it
      // honest rather than this comment.
      visibleTab: (tab: string) => tabVisible(hidden, tab),
      redacted: set.size > 0,
    };
  }, [hidden]);

  return (
    <VisibilityContext.Provider value={value}>{children}</VisibilityContext.Provider>
  );
}

/**
 * Does this tab still have anything on it?
 *
 * Exported as a plain function as well as a context method because the tab bar
 * is rendered ABOVE the provider — it has to be, since the provider's input is
 * the report and the bar exists before the first poll. Same logic either way,
 * so the bar and the cards under it cannot disagree.
 *
 * Section ids are `<tab>.<thing>` by convention (see api/sections.py), so
 * membership is a prefix test and the frontend needs no copy of the catalogue
 * beyond the id list PISR.tsx registers. `api/tests/test_sections.py` asserts
 * that convention holds — without it this silently answers "visible" for every
 * tab, which is the safe direction but not the intended one.
 */
export function tabVisible(hidden: string[] | null | undefined, tab: string): boolean {
  if (!hidden || !hidden.length) return true;
  const prefix = `${tab}.`;
  let hiddenHere = 0;
  for (const id of hidden) if (id.startsWith(prefix)) hiddenHere += 1;
  // A tab goes only when every one of its sections has. countOnTab answers
  // Infinity for a tab nobody registered, so an unknown tab is always shown —
  // an empty tab is a much smaller problem than a tab that vanishes because
  // this file had not heard of it.
  return hiddenHere < countOnTab(tab);
}


/**
 * How many sections live on a tab.
 *
 * Filled in by PISR.tsx at module load from the ids it actually renders, so
 * this file needs no hardcoded copy of the catalogue and cannot fall out of
 * step with the cards. Unregistered tabs answer Infinity, which makes
 * `visibleTab` return true — the safe direction, per the note above.
 */
const TAB_SIZES = new Map<string, number>();

export function registerSections(ids: readonly string[]) {
  TAB_SIZES.clear();
  for (const id of ids) {
    const tab = id.split(".")[0];
    TAB_SIZES.set(tab, (TAB_SIZES.get(tab) || 0) + 1);
  }
}

function countOnTab(tab: string): number {
  return TAB_SIZES.get(tab) ?? Number.POSITIVE_INFINITY;
}
