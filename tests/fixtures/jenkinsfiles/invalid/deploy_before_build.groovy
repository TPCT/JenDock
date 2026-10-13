// expect: line 6: not built by an earlier dockerBuild
pipeline {
    agent any
    stages {
        stage('Deploy') {
            steps { deploy service: 'app', image: 'app' }
        }
        stage('Build') {
            steps { dockerBuild(image: 'app') }
        }
    }
}
