use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};

use gix::odb::pack;

use super::Pack;

const OBJECT_HASH: gix::hash::Kind = gix::hash::Kind::Sha1;

#[derive(Debug, thiserror::Error)]
#[error("reading the pack")]
pub struct ReadError(#[source] gix::Error);

pub struct Objects {
    pack: Pack,
    data: pack::data::File<Arc<[u8]>>,
    index: pack::index::File<Vec<u8>>,
    inflate: Mutex<gix::zlib::Inflate>,
}

impl std::fmt::Debug for Objects {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Objects")
            .field("bytes", &self.pack.as_bytes().len())
            .field("objects", &self.index.num_objects())
            .finish()
    }
}

fn entry_bytes(range: pack::data::EntryRange, pack: &Arc<[u8]>) -> Option<&[u8]> {
    pack.get(range.start as usize..range.end as usize)
}

impl Objects {
    pub fn new(pack: Pack) -> Result<Objects, ReadError> {
        let bytes = pack.shared();
        let mut entries = pack::data::input::BytesToEntriesIter::new_from_header(
            &bytes[..],
            pack::data::input::Mode::Verify,
            pack::data::input::EntryDataMode::Crc32,
            OBJECT_HASH,
        )
        .map_err(ReadError)?;
        let pack_version = entries.version();
        let mut index = Vec::new();
        pack::index::write_data_iter_to_stream(
            pack::index::Version::default(),
            || {
                Ok((
                    entry_bytes as fn(pack::data::EntryRange, &Arc<[u8]>) -> Option<&[u8]>,
                    bytes.clone(),
                ))
            },
            &mut entries,
            Some(1),
            &mut gix::progress::Discard,
            &mut index,
            &AtomicBool::new(false),
            OBJECT_HASH,
            None,
            pack_version,
        )
        .map_err(ReadError)?;
        let index = pack::index::File::from_data(index, PathBuf::from("memory.idx"), OBJECT_HASH)
            .map_err(ReadError)?;
        let data = pack::data::File::from_data(bytes, PathBuf::from("memory.pack"), OBJECT_HASH)
            .map_err(ReadError)?;
        Ok(Objects {
            pack,
            data,
            index,
            inflate: Mutex::new(gix::zlib::Inflate::default()),
        })
    }

    pub fn pack(&self) -> &Pack {
        &self.pack
    }
}

impl gix::objs::Find for Objects {
    fn try_find<'a>(
        &self,
        id: &gix::hash::oid,
        buffer: &'a mut Vec<u8>,
    ) -> gix::Result<Option<gix::objs::Data<'a>>> {
        let Some(idx) = self.index.lookup(id) else {
            return Ok(None);
        };
        let entry = self.data.entry(self.index.pack_offset_at_index(idx))?;
        let mut inflate = self.inflate.lock().unwrap_or_else(|e| e.into_inner());
        let decoded = self.data.decode_entry(
            entry,
            buffer,
            &mut inflate,
            &|base, _| {
                self.index
                    .lookup(base)
                    .map(|idx| {
                        self.data
                            .entry(self.index.pack_offset_at_index(idx))
                            .map(pack::data::decode::entry::ResolvedBase::InPack)
                    })
                    .transpose()
            },
            &mut pack::cache::Never,
        )?;
        Ok(Some(gix::objs::Data::new(
            buffer,
            decoded.kind,
            OBJECT_HASH,
        )))
    }
}
