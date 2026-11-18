/* prd/32: the build identity line ("v0.9.0 · 88f72bb").
 *
 * `GET /api/meta` returns {version, tag, sha, build_time}; the tag and SHA are
 * rendered separately so a build that predates the first release tag (SHA only)
 * and one with no git metadata (a baked VERSION file) both read honestly. Pure
 * helpers, no DOM, so the node harness can test the exact strings.
 */

/** The label for a /api/meta payload: tag · sha, or whichever one exists. */
export function versionLabel(meta) {
  if (!meta) return '';
  const tag = String(meta.tag || '').trim();
  const sha = String(meta.sha || '').trim();
  if (tag && sha) return `${tag} · ${sha}`;
  if (tag) return tag;
  if (sha) return sha;
  return String(meta.version || '').trim();
}

/** The tooltip: the full describe string plus when the build was made. */
export function versionTitle(meta) {
  if (!meta) return 'build identity';
  const full = String(meta.version || '').trim();
  const when = String(meta.build_time || '').trim();
  const source = meta.source === 'git'
    ? 'git describe'
    : (meta.source === 'version-file' ? 'baked VERSION file' : 'built-in version');
  return `build ${full || '—'} (${source})${when ? ` · built ${when}` : ''}`;
}
