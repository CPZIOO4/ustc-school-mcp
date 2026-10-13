//! Byte-compatible with existing Windows DPAPI files and encrypted SQLite rows.
use crate::{Error, Result};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

fn failure() -> Error {
    Error::new(
        "storage_failure",
        "本机加密记录无法读写；请检查磁盘并使用原 Windows 账户。",
    )
}
#[cfg(windows)]
pub fn crypt(bytes: &[u8], decrypt: bool) -> Result<Vec<u8>> {
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::Cryptography::{CryptProtectData, CryptUnprotectData, CRYPT_INTEGER_BLOB},
    };
    let source = CRYPT_INTEGER_BLOB {
        cbData: u32::try_from(bytes.len()).map_err(|_| failure())?,
        pbData: bytes.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: std::ptr::null_mut(),
    };
    // Win32 allocates the output. Always free it, and forbid UI prompts.
    unsafe {
        let ok = if decrypt {
            CryptUnprotectData(
                &source,
                std::ptr::null_mut(),
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null(),
                1,
                &mut output,
            )
        } else {
            CryptProtectData(
                &source,
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null(),
                1,
                &mut output,
            )
        };
        if ok == 0 {
            return Err(failure());
        }
        let result = if output.cbData == 0 {
            Vec::new()
        } else {
            std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec()
        };
        LocalFree(output.pbData as *mut _);
        Ok(result)
    }
}
#[cfg(not(windows))]
pub fn crypt(_bytes: &[u8], _decrypt: bool) -> Result<Vec<u8>> {
    Err(Error::new(
        "unsupported_platform",
        "此版本的加密记录仅支持原 Windows 用户；其他平台密钥存储尚待接入。",
    ))
}
fn path(value: &Value, field: &str) -> Result<PathBuf> {
    let result = PathBuf::from(value[field].as_str().ok_or_else(Error::invalid)?);
    if !result.is_absolute() {
        return Err(Error::invalid());
    }
    Ok(result)
}
fn bounded_read(path: &Path) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(|_| failure())?
        .take(12 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| failure())?;
    if bytes.len() > 12 * 1024 * 1024 {
        return Err(failure());
    }
    Ok(bytes)
}
fn decode(bytes: &[u8]) -> Result<Value> {
    let raw = crypt(bytes, true)?;
    crate::strict_json::parse(&raw).map_err(|_| failure())
}
#[cfg(windows)]
fn replace(source: &Path, target: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let source: Vec<_> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<_> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    if unsafe {
        MoveFileExW(
            source.as_ptr(),
            target.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(failure());
    }
    Ok(())
}
#[cfg(not(windows))]
fn replace(source: &Path, target: &Path) -> Result<()> {
    fs::rename(source, target).map_err(|_| failure())
}
pub fn private_read(input: &Value) -> Result<Value> {
    let path = path(input, "path")?;
    if !path.try_exists().map_err(|_| failure())? {
        return Ok(input["default"].clone());
    }
    decode(&bounded_read(&path)?)
}
pub fn private_write(input: &Value) -> Result<Value> {
    let path = path(input, "path")?;
    let raw = serde_json::to_vec(input.get("value").ok_or_else(Error::invalid)?)
        .map_err(|_| failure())?;
    if raw.len() > 8 * 1024 * 1024 {
        return Err(failure());
    }
    let ciphertext = crypt(&raw, false)?;
    let parent = path.parent().ok_or_else(failure)?;
    fs::create_dir_all(parent).map_err(|_| failure())?;
    let temp = parent.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .map_err(|_| failure())?;
        file.write_all(&ciphertext).map_err(|_| failure())?;
        file.sync_all().map_err(|_| failure())?;
        drop(file);
        replace(&temp, &path)
    })();
    // This generated temporary path is the only file removed after failure.
    let _ = fs::remove_file(&temp);
    result?;
    Ok(json!({"saved":true}))
}
fn database(input: &Value) -> Result<rusqlite::Connection> {
    let path = path(input, "database")?;
    let db = rusqlite::Connection::open(path).map_err(|_| failure())?;
    db.busy_timeout(Duration::from_secs(10))
        .map_err(|_| failure())?;
    db.execute(
        "CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, payload BLOB NOT NULL)",
        [],
    )
    .map_err(|_| failure())?;
    Ok(db)
}
fn chat_id(value: &Value) -> Result<&str> {
    value
        .as_str()
        .filter(|s| {
            s.len() == 32
                && s.bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
        .ok_or_else(|| Error::new("invalid_arguments", "无效的对话编号。"))
}
pub fn chat_save(input: &Value) -> Result<Value> {
    let mut chat = input["chat"].clone();
    let id = chat_id(&chat["id"])?.to_owned();
    let updated_at = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    chat["updated_at"] = json!(updated_at);
    let raw = serde_json::to_vec(&chat).map_err(|_| failure())?;
    if raw.len() > 8 * 1024 * 1024 {
        return Err(Error::new(
            "storage_capacity",
            "对话已达到本机记录上限，请建立新的对话。",
        ));
    }
    let ciphertext = crypt(&raw, false)?;
    database(input)?
        .execute(
            "INSERT OR REPLACE INTO chats VALUES (?,?)",
            rusqlite::params![id, ciphertext],
        )
        .map_err(|_| failure())?;
    Ok(json!({"updated_at":updated_at}))
}
pub fn chat_read(input: &Value) -> Result<Value> {
    use rusqlite::OptionalExtension;
    let id = chat_id(&input["id"])?;
    let bytes: Option<Vec<u8>> = database(input)?
        .query_row("SELECT payload FROM chats WHERE id=?", [id], |row| {
            row.get(0)
        })
        .optional()
        .map_err(|_| failure())?;
    decode(&bytes.ok_or_else(|| Error::new("invalid_arguments", "对话不存在。"))?)
}
pub fn chat_list(input: &Value) -> Result<Value> {
    let db = database(input)?;
    let mut statement = db
        .prepare("SELECT payload FROM chats ORDER BY rowid DESC LIMIT 50")
        .map_err(|_| failure())?;
    let rows = statement
        .query_map([], |row| row.get::<_, Vec<u8>>(0))
        .map_err(|_| failure())?;
    let mut values = Vec::new();
    for row in rows {
        let chat = decode(&row.map_err(|_| failure())?)?;
        values.push(json!({"id":chat["id"], "title":chat["title"], "updated_at":chat["updated_at"], "state":chat["state"]}));
    }
    Ok(json!(values))
}
