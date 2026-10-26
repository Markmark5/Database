/**
 * Weekly Google Drive backup of the Database repo (Markmark5/Database).
 *
 * Downloads database.json, every archive/*.json, the calendar/ data files and any
 * scorecards_html/ originals, zips them, and saves the zip to a Drive folder.
 * Keeps the newest KEEP_WEEKS zips and deletes older ones.
 *
 * SETUP (once, about 5 minutes)
 *  1. Go to https://script.google.com → New project. Name it "Database backup".
 *  2. Delete the sample code, paste this whole file, click Save (💾).
 *  3. Choose the function "backupNow" in the toolbar and click Run.
 *     Approve the permissions (Drive + external requests). Check Drive for the
 *     folder "Database Backups" with today's zip in it.
 *  4. Choose "installWeeklyTrigger" and click Run once. That sets the weekly timer
 *     (Sunday ~11pm UK). To stop it, run "removeTriggers".
 *  5. Optional: View → Executions shows each run; failures email you by default.
 */

var REPO = 'Markmark5/Database';
var BRANCH = 'main';
var FOLDER_NAME = 'Database Backups';
var KEEP_WEEKS = 12;
/* folders copied in full (only .json / .html / .csv files, no sub-folders) */
var FOLDERS = ['archive', 'calendar', 'data', 'scorecards_html'];
/* single files at the repo root */
var ROOT_FILES = ['database.json'];

function backupNow() {
  var stamp = Utilities.formatDate(new Date(), 'Europe/London', 'yyyy-MM-dd');
  var paths = ROOT_FILES.slice();
  FOLDERS.forEach(function (f) { paths = paths.concat(listFolder_(f)); });

  var blobs = [], missing = [];
  paths.forEach(function (p) {
    var url = 'https://raw.githubusercontent.com/' + REPO + '/' + BRANCH + '/' + p.split('/').map(encodeURIComponent).join('/');
    var r = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (r.getResponseCode() !== 200) { missing.push(p + ' (' + r.getResponseCode() + ')'); return; }
    blobs.push(r.getBlob().setName(p));
  });
  if (!blobs.some(function (b) { return b.getName() === 'database.json'; })) {
    throw new Error('Backup aborted: database.json could not be downloaded. ' + missing.join(', '));
  }

  var zip = Utilities.zip(blobs, 'Database_backup_' + stamp + '.zip');
  var folder = getFolder_();
  var old = folder.getFilesByName(zip.getName());
  while (old.hasNext()) old.next().setTrashed(true);   /* re-running the same day replaces that day's zip */
  folder.createFile(zip);
  prune_(folder);

  Logger.log('Saved ' + zip.getName() + ' — ' + blobs.length + ' files' + (missing.length ? '; skipped: ' + missing.join(', ') : ''));
}

/* files directly in a repo folder (GitHub API, no key needed for a public repo) */
function listFolder_(folder) {
  var r = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/contents/' + folder + '?ref=' + BRANCH,
    { muteHttpExceptions: true, headers: { 'Accept': 'application/vnd.github.v3+json' } });
  if (r.getResponseCode() !== 200) return [];   /* folder not there yet (e.g. scorecards_html) */
  return JSON.parse(r.getContentText())
    .filter(function (f) { return f.type === 'file' && /\.(json|html|csv)$/i.test(f.name); })
    .map(function (f) { return f.path; });
}

function getFolder_() {
  var it = DriveApp.getFoldersByName(FOLDER_NAME);
  return it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER_NAME);
}

function prune_(folder) {
  var files = [], it = folder.getFiles();
  while (it.hasNext()) { var f = it.next(); if (/^Database_backup_\d{4}-\d{2}-\d{2}\.zip$/.test(f.getName())) files.push(f); }
  files.sort(function (a, b) { return b.getName().localeCompare(a.getName()); });
  files.slice(KEEP_WEEKS).forEach(function (f) { f.setTrashed(true); });
}

function installWeeklyTrigger() {
  removeTriggers();
  ScriptApp.newTrigger('backupNow').timeBased().onWeekDay(ScriptApp.WeekDay.SUNDAY).atHour(23)
    .inTimezone('Europe/London').create();
}

function removeTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'backupNow') ScriptApp.deleteTrigger(t);
  });
}
