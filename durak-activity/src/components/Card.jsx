import React from 'react'
import './Card.css'

const Card = ({ card, onClick, selected, disabled, size = 'normal' }) => {
  const { rank, suit, isRed } = card

  const sizeClasses = {
    small: 'card-small',
    normal: 'card-normal',
    large: 'card-large'
  }

  return (
    <div
      className={`card ${sizeClasses[size]} ${selected ? 'selected' : ''} ${disabled ? 'disabled' : ''}`}
      onClick={!disabled ? onClick : undefined}
    >
      <div className={`card-inner ${isRed ? 'red' : 'black'}`}>
        <div className="card-corner top-left">
          <span className="rank">{rank}</span>
          <span className="suit">{suit}</span>
        </div>
        <div className="card-center">
          <span className="suit-large">{suit}</span>
        </div>
        <div className="card-corner bottom-right">
          <span className="rank">{rank}</span>
          <span className="suit">{suit}</span>
        </div>
      </div>
    </div>
  )
}

export default Card
