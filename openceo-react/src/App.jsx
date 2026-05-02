import React, { useContext, useEffect, useState } from 'react'
import { Outlet } from 'react-router-dom'
import Loading from './components/Loading'
import Overlay from './components/Overlay'
import { StoreContext } from './context/store'

function App() {
  const store = useContext(StoreContext)
  const [showOverlay, setShowOverlay] = useState(false)
  const [showLoading, setShowLoading] = useState(false)

  useEffect(() => {
    let timer
    if (store.loading) {
      setShowOverlay(true)
      timer = setTimeout(() => {
        setShowLoading(true)
      }, 1000)
    } else {
      setShowLoading(false)
      setShowOverlay(false)
      clearTimeout(timer)
    }
    return () => clearTimeout(timer)
  }, [store.loading])

  return (
    <React.Fragment>
      {showOverlay && <Overlay />}
      {showLoading && <Loading />}
      <Outlet />
    </React.Fragment>
  )
}

export default App
