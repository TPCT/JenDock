// expect: line 2: @Library are not supported
@Library('shared') _
pipeline {
    agent any
    stages { stage('A') { steps { echo 'x' } } }
}
