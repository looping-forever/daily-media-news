/**
 * functions/_middleware.js
 *
 * The digest archive is PUBLIC by design. This middleware used to gate every
 * static asset behind a username/password prompt; that gate was removed on
 * 2026-10-30 because the owner could not open the site
 * from a phone's in-app browser, where the native credential dialog is
 * suppressed. The repository is public, so every digest HTML was already
 * readable anonymously from GitHub and the password protected nothing.
 *
 * The file stays as a pass-through so the Pages deploy contract still finds a
 * functions directory at the repo root. It adds no headers and no routes: it
 * simply lets every request reach the static assets.
 */
export async function onRequest(context) {
  return await context.next();
}
