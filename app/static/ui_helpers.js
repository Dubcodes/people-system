(() => {
  const SETTINGS_KEY = 'peopleSystemSettings';
  const CUSTOM_HELP = {
    Operator: 'For team operators to see what the main viewer has in the quick look tile, and to select a tile for the viewer to see highlighted in orange.',
    Viewer: 'Open the main viewer used for fast race-day face and name recognition.',
    Settings: 'Open display settings, theme colors, date override, manual attendees, and local race summaries.',
    Help: 'Open the help guide.',
    'Image Ingest': 'Add new photos and tag them to people, roles, stables, horses, and photo locations.',
    'Manage People': 'Open the records page for editing people, images, roles, stables, notes, and linked data.',
    Horses: 'Open horse records, imported race history, stable context, and manual horse/person links.',
    'Import Races': 'Paste TAB race fields and import race-day runners, jockeys, trainers, horses, and horse numbers.',
    'Race Management': 'Inspect imported race days, runners, silk data, and locked cleanup tools.',
    LOVERACING: 'Discover, preview, import, audit, and undo official LOVERACING race-day data.',
    Admin: 'Open master-only backups, audit, table editor, track aliases, and PIN maintenance.',
    'Single Image': 'Use the single-image ingest workflow.',
    'Bulk Queue': 'Use the shared pending bulk-image queue.',
    'Upload To Pending': 'Put checked local images into the shared pending queue. They stay unassigned unless you choose a Pending region on the row.',
    'Process Selected': 'Save checked review rows into person records after you have checked the suggested names and metadata.',
    'Clear Saved': 'Remove rows that have already been saved or moved out of the review list.',
    'Refresh Pending': 'Reload the shared pending queue.',
    'Load Into Review': 'Bring this pending image into the review list so it can be checked and saved.',
    'Save Region': 'Save this pending image assignment, including setting it back to Unassigned.',
    Skip: 'Mark this pending image as not useful without saving it to a person record.',
    Preview: 'Parse and show the data before writing it to the system.',
    Import: 'Write the checked or previewed data into the system.',
  };

  function settings() {
    try {
      return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    } catch (_) {
      return {};
    }
  }

  function hoverHelpEnabled() {
    return settings().hoverHelp !== false;
  }

  function applyTheme() {
    const theme = settings().theme || {};
    Object.entries(theme).forEach(([key, value]) => {
      if (value) document.documentElement.style.setProperty(`--${key}`, value);
    });
  }

  function labelFor(element) {
    return (element.dataset.help || element.getAttribute('aria-label') || element.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function generatedHelp(element) {
    if (element.dataset.help) return element.dataset.help.trim();
    const label = labelFor(element);
    if (!label) return '';
    if (CUSTOM_HELP[label]) return CUSTOM_HELP[label];
    if (element.tagName === 'A') return `Open ${label}.`;
    if (/delete|remove/i.test(label)) return `${label}. This may permanently remove data, so the app will ask for confirmation where needed.`;
    if (/save|update/i.test(label)) return `${label}. The app will ask you to confirm meaningful changes before applying them.`;
    if (/^use\s+/i.test(label)) return `${label}. Fill the row from this suggested match.`;
    if (/clear|reset/i.test(label)) return `${label}.`;
    if (/all|jockey|trainer|presenter|staff|stable rep|has image|no image/i.test(label)) return `Filter the list by ${label}.`;
    return `${label}.`;
  }

  function applyHoverHelp(root = document) {
    root.querySelectorAll('button, .button-link, [data-help]').forEach(element => {
      if (!hoverHelpEnabled()) {
        if (element.dataset.autoHelp === 'true') element.removeAttribute('title');
        return;
      }
      const help = generatedHelp(element);
      if (help) {
        element.setAttribute('title', help);
        element.dataset.autoHelp = 'true';
      }
    });
  }

  function pressStart(event) {
    const target = event.target.closest('button, .button-link');
    if (target) target.classList.add('is-pressing');
  }

  function pressEnd() {
    document.querySelectorAll('.is-pressing').forEach(element => element.classList.remove('is-pressing'));
  }

  function installPressStyles() {
    const style = document.createElement('style');
    style.textContent = `
      button, .button-link { transform-origin:center center; }
      .button-link[aria-label="Help"], a[href="/help.html"][aria-label="Help"] {
        width: 28px !important;
        min-width: 28px !important;
        height: 28px !important;
        min-height: 28px !important;
        padding: 0 !important;
        border-radius: 50% !important;
        border-color: rgba(80, 88, 96, .14) !important;
        background: rgba(255, 255, 255, .18) !important;
        color: rgba(83, 92, 101, .68) !important;
        box-shadow: none !important;
        font-size: 14px !important;
        font-weight: 700 !important;
        line-height: 1 !important;
        opacity: .72;
      }
      .button-link[aria-label="Help"]:hover, a[href="/help.html"][aria-label="Help"]:hover {
        background: rgba(255, 255, 255, .34) !important;
        color: rgba(63, 72, 81, .84) !important;
        opacity: .9;
      }
      button.is-pressing, .button-link.is-pressing {
        transform: translateY(2px) scale(.94) !important;
        filter: brightness(.78) saturate(1.18) !important;
        box-shadow: inset 0 3px 12px rgba(0,0,0,.24), 0 0 0 2px rgba(0,0,0,.08) !important;
      }
      button:active, .button-link:active {
        transform: translateY(2px) scale(.94) !important;
        filter: brightness(.78) saturate(1.18) !important;
        box-shadow: inset 0 3px 12px rgba(0,0,0,.24), 0 0 0 2px rgba(0,0,0,.08) !important;
      }
    `;
    document.head.appendChild(style);
  }

  function normaliseNav() {
    const navs = document.querySelectorAll('.nav');
    if (!navs.length) return;
    const path = window.location.pathname.replace(/\/$/, '') || '/';
    const items = [
      ['Viewer', '/'],
      ...(path === '/' ? [['Operator', '/operator']] : []),
      ['Image Ingest', '/ingest'],
      ['Bulk Ingest', '/bulk-ingest'],
      ['Manage People', '/records'],
      ['Horses', '/horses'],
      ['Import Races', '/race-ingest'],
      ['Race Management', '/race-management'],
      ['LOVERACING', '/loveracing'],
      ['Settings', '/settings'],
      ['Admin', '/master-admin'],
      ['?', '/help.html', 'Help'],
    ];
    const isActive = href => {
      const cleanHref = href.replace(/\/$/, '') || '/';
      if (path === cleanHref) return true;
      return href === '/help.html' && (path === '/help' || path === '/help.html');
    };
    const globalNav = items.map(([label, href, aria]) => {
      const active = isActive(href) ? ' primary' : '';
      const ariaText = aria ? ` aria-label="${aria}" title="${aria}"` : '';
      return `<a class="button-link${active}" href="${href}"${ariaText}>${label}</a>`;
    }).join('');
    navs.forEach(nav => {
      nav.innerHTML = globalNav;
    });
  }

  window.peopleSystemApplyHoverHelp = applyHoverHelp;

  window.addEventListener('DOMContentLoaded', () => {
    applyTheme();
    normaliseNav();
    installPressStyles();
    applyHoverHelp();
    document.addEventListener('pointerdown', pressStart);
    document.addEventListener('pointerup', pressEnd);
    document.addEventListener('pointercancel', pressEnd);
    document.addEventListener('mouseleave', pressEnd);

    const observer = new MutationObserver(mutations => {
      mutations.forEach(mutation => {
        mutation.addedNodes.forEach(node => {
          if (node.nodeType === Node.ELEMENT_NODE) applyHoverHelp(node);
        });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  });
})();
