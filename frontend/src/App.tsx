import { useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { Loading } from './components/ui'
import AlbumDetail from './pages/AlbumDetail'
import Albums from './pages/Albums'
import Admin from './pages/Admin'
import Analytics from './pages/Analytics'
import ArtistDetail from './pages/ArtistDetail'
import Artists from './pages/Artists'
import Discover from './pages/Discover'
import Home from './pages/Home'
import Login from './pages/Login'
import PlaylistDetail from './pages/PlaylistDetail'
import Playlists from './pages/Playlists'
import Podcasts from './pages/Podcasts'
import Search from './pages/Search'
import Settings from './pages/Settings'
import Tracks from './pages/Tracks'
import { useAuth } from './store/auth'

export default function App() {
  const { user, loading, bootstrap } = useAuth()

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loading label="Starting Musicdrome" />
      </div>
    )
  }

  if (!user) {
    return (
      <Routes>
        <Route path="*" element={<Login />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Home />} />
        <Route path="/albums" element={<Albums />} />
        <Route path="/albums/:id" element={<AlbumDetail />} />
        <Route path="/artists" element={<Artists />} />
        <Route path="/artists/:id" element={<ArtistDetail />} />
        <Route path="/tracks" element={<Tracks />} />
        <Route path="/playlists" element={<Playlists />} />
        <Route path="/playlists/:id" element={<PlaylistDetail />} />
        <Route path="/search" element={<Search />} />
        <Route path="/discover" element={<Discover />} />
        <Route path="/podcasts" element={<Podcasts />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="/settings" element={<Settings />} />
        {user.is_admin && <Route path="/admin" element={<Admin />} />}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
