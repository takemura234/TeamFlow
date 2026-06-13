function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    const properties = PropertiesService.getScriptProperties();
    const expectedToken = properties.getProperty('TEAMFLOW_BACKUP_TOKEN');
    const folderId = properties.getProperty('TEAMFLOW_BACKUP_FOLDER_ID');
    if (!expectedToken || data.token !== expectedToken) {
      return jsonResponse({ ok: false, error: 'Unauthorized' });
    }
    if (!folderId || !data.filename || !data.content_base64) {
      return jsonResponse({ ok: false, error: 'Backup settings or data are missing' });
    }

    const folder = DriveApp.getFolderById(folderId);
    const bytes = Utilities.base64Decode(data.content_base64);
    folder.createFile(Utilities.newBlob(bytes, 'application/x-sqlite3', data.filename));

    const keep = Math.max(1, Math.min(Number(data.keep) || 30, 100));
    const files = [];
    const iterator = folder.getFiles();
    while (iterator.hasNext()) {
      const file = iterator.next();
      if (/^teamflow-\d{8}-\d{6}-\d+\.db$/.test(file.getName())) files.push(file);
    }
    files.sort((a, b) => b.getDateCreated().getTime() - a.getDateCreated().getTime());
    files.slice(keep).forEach(file => file.setTrashed(true));
    return jsonResponse({ ok: true, filename: data.filename, retained: Math.min(files.length, keep) });
  } catch (error) {
    return jsonResponse({ ok: false, error: String(error) });
  }
}

function jsonResponse(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
