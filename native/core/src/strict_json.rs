//! Reject duplicate keys at every depth before a model can dispatch any tool.
use serde::{
    de::{self, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer,
};
use serde_json::{Map, Number, Value};
use std::fmt;

struct Strict(Value);
impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Strict;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("strict JSON")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Strict, E> {
                Number::from_f64(v)
                    .map(|n| Strict(Value::Number(n)))
                    .ok_or_else(|| E::custom("finite number required"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Strict, E> {
                Ok(Strict(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Strict, E> {
                Ok(Strict(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Strict, A::Error> {
                let mut values = Vec::new();
                while let Some(Strict(v)) = a.next_element()? {
                    values.push(v);
                }
                Ok(Strict(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Strict, A::Error> {
                let mut values = Map::new();
                while let Some(k) = a.next_key::<String>()? {
                    if values.contains_key(&k) {
                        return Err(de::Error::custom("duplicate key"));
                    }
                    let Strict(v) = a.next_value()?;
                    values.insert(k, v);
                }
                Ok(Strict(Value::Object(values)))
            }
        }
        d.deserialize_any(V)
    }
}
pub fn parse(raw: &[u8]) -> Result<Value, serde_json::Error> {
    // serde_json otherwise rounds integer literals outside i64/u64 into f64.
    // Fail closed rather than silently changing an identifier or amount.
    let mut i = 0;
    let mut quoted = false;
    while i < raw.len() {
        if quoted {
            if raw[i] == b'\\' {
                i += 2;
                continue;
            }
            if raw[i] == b'"' {
                quoted = false;
            }
        } else if raw[i] == b'"' {
            quoted = true;
        } else if raw[i] == b'-' || raw[i].is_ascii_digit() {
            let start = i;
            while i < raw.len()
                && (raw[i].is_ascii_digit() || matches!(raw[i], b'.' | b'e' | b'E' | b'+' | b'-'))
            {
                i += 1;
            }
            let token = &raw[start..i];
            if !token.iter().any(|c| matches!(c, b'.' | b'e' | b'E')) {
                let text = std::str::from_utf8(token).unwrap_or("");
                if text.parse::<i64>().is_err() && text.parse::<u64>().is_err() {
                    return Err(serde_json::Error::io(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "integer out of range",
                    )));
                }
            }
            continue;
        }
        i += 1;
    }
    serde_json::from_slice::<Strict>(raw).map(|v| v.0)
}
