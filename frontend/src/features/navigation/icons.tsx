import {
  Activity, ChartNoAxesColumn, Check, ChevronDown, ChevronRight, CircleAlert, CircleSlash,
  CircleUser, FileText, Image as ImageIcon, Info, Library, LogOut, Menu, MessageSquareText,
  Minus, PanelLeftClose, PanelLeftOpen, Quote, ScrollText, Search, Settings, SlidersHorizontal,
  SquarePen, Stethoscope, TriangleAlert, X,
  type LucideIcon,
} from 'lucide-react';

/**
 * One icon set, at one size, with one stroke weight.
 *
 * The rail previously drew its navigation with typographic characters — a diamond, a gear
 * character, a box-drawing glyph. Those render at whatever weight and baseline the installed font
 * happens to give them, which is why they read as placeholder text rather than as a designed set.
 * These are outline SVGs from one family, so they share a stroke and a grid.
 *
 * Every icon here is decorative. An icon that names something has a text label beside it — visible
 * when the rail is open, `.visually-hidden` when it is collapsed — and that label is what supplies
 * the accessible name. Marking the SVG `aria-hidden` is therefore not a shortcut: it is what stops
 * a screen reader announcing the picture twice.
 */

export type IconName = keyof typeof ICONS;

const ICONS = {
  ask: MessageSquareText,
  library: Library,
  settings: Settings,
  'new-conversation': SquarePen,
  advanced: SlidersHorizontal,
  retrieval: Search,
  evaluations: ChartNoAxesColumn,
  operations: Activity,
  audit: ScrollText,
  'collapse-rail': PanelLeftClose,
  'expand-rail': PanelLeftOpen,
  menu: Menu,
  user: CircleUser,
  'sign-out': LogOut,
  brand: Stethoscope,
  source: Quote,
  document: FileText,
  figure: ImageIcon,
  info: Info,
  'chevron-down': ChevronDown,
  'chevron-right': ChevronRight,

  // Outcome and stage marks. Each is decorative and each sits beside its own word, so the state
  // is legible in greyscale, to a colour-blind reader and to a screen reader.
  verified: Check,
  'no-answer': CircleSlash,
  conflict: TriangleAlert,
  unverified: TriangleAlert,
  failed: CircleAlert,
  scope: Info,
  'stage-done': Check,
  'stage-skipped': Minus,
  'stage-failed': X,
} satisfies Record<string, LucideIcon>;

export function Icon({ name, small }: { name: IconName; small?: boolean }) {
  const Glyph = ICONS[name];
  return <Glyph
    className={small ? 'icon-sm' : 'icon'}
    aria-hidden="true"
    focusable="false"
    // Set explicitly rather than left to the library's defaults, so the size cannot drift if the
    // dependency changes its own.
    size={small ? 15 : 18}
    strokeWidth={1.75}
  />;
}
