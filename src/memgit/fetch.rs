use std::io::Read as _;
use std::ops::ControlFlow;

use gix::protocol::fetch as wire;
use gix::protocol::transport;
use gix::protocol::transport::client::blocking_io::{self, ExtendedBufRead as _};

use super::Pack;

const MISSING_COMMIT: [&str; 3] = [
    "not our ref",
    "couldn't find remote ref",
    "no such remote ref",
];

type Authenticate = Box<
    dyn FnMut(
            gix::credentials::helper::Action,
        ) -> gix::Result<Option<gix::credentials::protocol::Outcome>>
        + Send,
>;

#[derive(Debug, thiserror::Error)]
pub enum FetchError {
    #[error("the remote URL is invalid")]
    Url(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("connecting to the remote")]
    Connect(#[source] gix::Error),
    #[error("greeting the remote")]
    Handshake(#[source] gix::Error),
    #[error("the remote lacks a feature a fetch needs")]
    Features(#[source] gix::Error),
    #[error("the remote cannot send commits without their history")]
    NoShallow,
    #[error("sending the fetch request")]
    Send(#[source] transport::client::Error),
    #[error("reading the remote's reply")]
    Response(#[source] gix::Error),
    #[error("a commit is no longer available on the remote")]
    CommitUnavailable(#[source] gix::Error),
    #[error("the remote sent no pack")]
    NoPack,
    #[error("reading the pack")]
    Read(#[source] std::io::Error),
}

pub struct Remote {
    url: gix::Url,
    authenticate: Authenticate,
}

impl Remote {
    pub fn at<Url, E>(url: Url) -> Result<Remote, FetchError>
    where
        Url: TryInto<gix::Url, Error = E>,
        E: std::error::Error + Send + Sync + 'static,
    {
        Ok(Remote {
            url: url.try_into().map_err(|e| FetchError::Url(Box::new(e)))?,
            authenticate: Box::new(|_| Ok(None)),
        })
    }

    pub fn with_credentials(
        mut self,
        helper: impl FnMut(
            gix::credentials::helper::Action,
        ) -> gix::Result<Option<gix::credentials::protocol::Outcome>>
        + Send
        + 'static,
    ) -> Remote {
        self.authenticate = Box::new(helper);
        self
    }

    pub fn fetch(mut self, commits: &[gix::hash::ObjectId]) -> Result<Pack, FetchError> {
        let mut transport = blocking_io::connect::connect(
            self.url,
            blocking_io::connect::Options {
                version: transport::Protocol::V2,
                ..Default::default()
            },
        )
        .map_err(FetchError::Connect)?;
        let handshake = gix::protocol::handshake(
            &mut transport,
            transport::Service::UploadPack,
            &mut self.authenticate,
            Vec::new(),
            &mut gix::progress::Discard,
        )
        .map_err(FetchError::Handshake)?;

        let version = handshake.server_protocol_version;
        let mut features =
            gix::protocol::Command::Fetch.default_features(version, &handshake.capabilities);
        features.push(("agent", Some(gix::protocol::agent(gix::env::agent()))));
        wire::Response::check_required_features(version, &features)
            .map_err(FetchError::Features)?;
        let mut arguments = wire::Arguments::new(version, features, false);
        if !arguments.can_use_shallow() {
            return Err(FetchError::NoShallow);
        }
        for commit in commits {
            arguments.want(commit);
        }
        arguments.deepen(1);
        let fetched = (|| -> Result<Vec<u8>, FetchError> {
            let mut reader = arguments
                .send(&mut transport, true)
                .map_err(FetchError::Send)?;
            let response = wire::Response::from_line_reader(version, &mut reader, true, false)
                .map_err(|e| {
                    let detail = format!("{e:?}");
                    if MISSING_COMMIT.iter().any(|phrase| detail.contains(phrase)) {
                        FetchError::CommitUnavailable(e)
                    } else {
                        FetchError::Response(e)
                    }
                })?;
            if !response.has_pack() {
                return Err(FetchError::NoPack);
            }
            reader.set_progress_handler(Some(Box::new(|_, _| ControlFlow::Continue(()))));
            let mut pack = Vec::new();
            reader.read_to_end(&mut pack).map_err(FetchError::Read)?;
            Ok(pack)
        })();
        let _ = gix::protocol::indicate_end_of_interaction(&mut transport, false);
        match fetched? {
            pack if pack.is_empty() => Err(FetchError::NoPack),
            pack => Ok(Pack::from(pack)),
        }
    }
}
